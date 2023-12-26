import collections
import configparser
import functools
import logging
import math
import os
import re
import string

import numpy as np
import pandas as pd
import scipy.sparse
from fuzzywuzzy import fuzz

from rcounting import counters, parsing
from rcounting import thread_navigation as tn
from rcounting import utils
from rcounting.models import comment_to_dict
from rcounting.units import DAY, HOUR, MINUTE

printer = logging.getLogger(__name__)

alphanumeric = string.digits + string.ascii_uppercase


class CountingRule:
    """
    A rules class. It knows how to do two things:

    - Get enough history to see whether a given comment is valid
    - Determine whether all counts in a history of counts are valid.

    Examples of things it's intended to validate are:
      - That users waited enough time since their own last comment before commenting again
      - That users waited enough time since the global last comment
      - That users let enough other counters go before them
    """

    def __init__(self, wait_n=1, thread_time=0, user_time=0):
        self.n = wait_n
        self.thread_time = thread_time
        self.user_time = user_time

    def _valid_skip(self, history):
        n = self.n if self.n is not None else len(history)
        history = history.reset_index()
        skips = history.groupby("username")["index"].diff()
        return skips.isna() | (skips > n)

    def _valid_thread_time(self, history):
        if not self.thread_time:
            return True
        elapsed_time = history["timestamp"].diff()
        valid_time = elapsed_time.isna() | (elapsed_time >= self.thread_time)
        return valid_time

    def _valid_user_time(self, history):
        if not self.user_time:
            return True
        elapsed_user_time = history.groupby("username")["timestamp"].diff()
        valid_user_time = elapsed_user_time.isna() | (elapsed_user_time >= self.user_time)
        return valid_user_time

    def is_valid(self, history):
        return (
            self._valid_skip(history)
            & self._valid_thread_time(history)
            & self._valid_user_time(history)
        )

    def get_history(self, comment):
        comments = comment.walk_up_tree(limit=self.n + 1)
        max_time = max(self.thread_time, self.user_time)
        while (
            not comments[-1].is_root
            and (comment.created_utc - comments[-1].created_utc) < max_time
        ):
            comments = comments[:-1] + comments[-1].walk_up_tree(limit=9)
        return pd.DataFrame([comment_to_dict(x) for x in comments[:0:-1]])


class FastOrSlow(CountingRule):
    """
    An special case of the rules class to account for a thread where the rule
    is not of the form 'wait at least X' before counting, but rather
    'wait at most X or at least Y'.

    """

    def __init__(self):
        super().__init__()

    def _valid_thread_time(self, history):
        elapsed_time = history["timestamp"].diff()
        valid_time = elapsed_time.isna() | (elapsed_time < 5 * MINUTE) | (elapsed_time >= HOUR)
        return valid_time


class OnlyDoubleCounting:
    """
    Only double counting is sufficiently strange that it gets its own class.

    A thread is valid if every user in the chain counts exactly twice in a row.
    """

    def is_valid(self, history):
        history = history.set_index("comment_id")
        history["mask"] = True
        unshifted = history.username.iloc[::2]
        up_shift = history.username.shift(-1).iloc[::2]
        up_mask = up_shift.isna() | (up_shift == unshifted)
        down_shift = history.username.shift().iloc[::2]
        down_mask = down_shift.isna() | (down_shift == unshifted)
        mask = up_mask if (up_mask.sum() > down_mask.sum()) else down_mask
        history.loc[mask.index, "mask"] = mask
        history.reset_index(inplace=True)
        return history["mask"]

    def get_history(self, comment):
        comments = comment.walk_up_tree(limit=2)[:0:-1]
        return pd.DataFrame([comment_to_dict(x) for x in comments])


def validate_from_character_list(valid_characters, strip_links=True):
    def looks_like_count(comment_body):
        body = comment_body.upper()
        if strip_links:
            body = parsing.strip_markdown_links(body)
        return any(character in body for character in valid_characters)

    return looks_like_count


def base_n(n=10, strip_links=True):
    return validate_from_character_list(alphanumeric[:n], strip_links)


def permissive(comment_body: str) -> bool:
    return True


base_10 = base_n(10)
balanced_ternary = validate_from_character_list("T-0+")
brainfuck = validate_from_character_list("><+-.,[]")
roman_numeral = validate_from_character_list("IVXLCDMↁↂↇ")
mayan_form = validate_from_character_list("Ø1234|-")
twitter_form = validate_from_character_list("@")
parentheses_form = validate_from_character_list("()")


def d20_form(comment_body):
    return "|" in comment_body and base_10(comment_body)


def reddit_username_form(comment_body):
    return "u/" in comment_body


def throwaway_form(comment_body):
    return (fuzz.partial_ratio("u/throwaway", comment_body) > 80) and base_10(comment_body)


def illion_form(comment_body):
    return fuzz.partial_ratio("illion", comment_body) > 80


planets = ["MERCURY", "VENUS", "EARTH", "MARS", "JUPITER", "SATURN", "URANUS", "NEPTUNE"]
planetary_octal_form = validate_from_character_list(planets)

colors = ["RED", "ORANGE", "YELLOW", "GREEN", "BLUE", "INDIGO", "VIOLET"]
rainbow_form = validate_from_character_list(colors)


def ignore_revivals(chain, was_revival):
    return chain if was_revival is None else [x for x, y in zip(chain, was_revival) if not y]


def make_title_updater(comment_to_count):
    @functools.wraps(comment_to_count)
    def wrapper(old_count, chain, was_revival=None):
        chain = ignore_revivals(chain, was_revival)
        title = chain[-1].title
        return comment_to_count(parsing.body_from_title(title))

    return wrapper


def by_ns_count(n):
    def comment_to_count(comment):
        count = parsing.find_count_in_text(comment)
        return int(count // n)

    return comment_to_count


def base_n_count(n: int):
    def comment_to_count(comment):
        return parsing.find_count_in_text(comment, base=n)

    return comment_to_count


def permutation_order(word, alphabet, ordered=False, no_leading_zeros=False):
    word_length = len(word)
    if word_length == 0:
        return 0
    index = alphabet.index(word[0])
    position = index - int(no_leading_zeros)
    n_digits = len(alphabet)
    prefix = [] if ordered else alphabet[:index]
    new_alphabet = prefix + alphabet[index + 1 :]
    if ordered:
        first_place_counts = sum(
            math.comb(n_digits - 1 - i, word_length - 1) for i in range(position)
        )
    else:
        first_place_counts = position * math.perm(n_digits - 1, word_length - 1)
    return first_place_counts + permutation_order(word[1:], new_alphabet, ordered=ordered)


def binary_coded_decimal_count(comment):
    count = f"{parsing.find_count_in_text(comment, base=2):b}"
    digits = [str(int("".join(y for y in x), 2)) for x in utils.chunked(count, 4)]
    return int("".join(digits))


def no_repeating_count(comment):
    normalized_comment = str(parsing.find_count_in_text(comment))
    result = 9 * sum(math.perm(9, i - 1) for i in range(1, len(normalized_comment)))
    return result + permutation_order(normalized_comment, string.digits, no_leading_zeros=True)


def powerball_count(comment):
    balls, powerball = parsing.normalize_comment(comment).split("+")
    balls = balls.split()
    alphabet = [str(x) for x in range(1, 70)]
    return permutation_order(balls, alphabet, ordered=True) * 26 + int(powerball) - 1


def no_successive_count(comment):
    word = str(parsing.find_count_in_text(comment))
    result = sum(9**i for i in range(1, len(word)))
    previous_i = "0"
    for ix, i in enumerate(word[:-1]):
        result += 9 ** (len(word) - ix - 1) * (int(i) - (i >= previous_i))
        previous_i = i
    return result


u_squares = [11035, 65039, 129003, 129002, 128998, 129001, 129000, 128999, 128997, 11036]
colored_squares_form = validate_from_character_list([chr(x) for x in u_squares])

collatz_dict = {}


def collatz(n):
    if n == 1:
        return 1
    if n in collatz_dict:
        return collatz_dict[n]
    if n % 2 == 0:
        return 1 + collatz(n // 2)
    return 1 + collatz(3 * n + 1)


def collatz_count(comment):
    regex = r".*\((\d+).*(\d+)\)"
    current, steps = map(int, re.search(regex, comment).groups())
    return sum(collatz(i) for i in range(1, current)) + steps


default_rule = CountingRule()

# an int, then a bracketed int, maybe with a plus or a minus after it
wave_regex = r"(-?\d+).*\((\d+)[\+-]?\)"
double_wave_regex = r"(-?\d+).*\((\d+)\).*\((\d+)\)"


def wave_count(comment):
    comment = parsing.normalize_comment(comment)
    match = re.search(wave_regex, comment)
    a, b = [int(x) for x in match.groups()]
    return 2 * b**2 - a


def increasing_type_count(n):
    regex = r"(-?\d+)" + r".*\((\d+)\)" * n

    def count(comment):
        comment = parsing.normalize_comment(comment)
        total = 0
        values = [int(x) for x in re.search(regex, comment).groups()]
        for ix, value in enumerate(values):
            total += triangle_n_dimension(ix + 1, value)
        return total

    return count


def triangle_n_dimension(n, value):
    if value == 1:
        return 0
    return math.comb(value - 2 + n, n)


def gaussian_integer_count(comment):
    digits = str(parsing.find_count_in_text(comment))
    corner = sum((-4) ** ix * int(digit) for ix, digit in enumerate(digits[::-2]))
    return (2 * corner + 1) ** 2


def update_dates(count, chain, was_revival=None):
    chain = ignore_revivals(chain, was_revival)[:-1]
    regex = r"([,\d]+)$"  # All digits at the end of the line, plus optional separators
    for submission in chain:
        year = int(re.search(regex, submission.title).group().replace(",", ""))
        length = 1095 + any(map(utils.is_leap_year, range(year, year + 3)))
        count += length
    return count


def update_from_traversal(count, chain, was_revival):
    for thread in chain[1:]:
        _, get_id = tn.find_previous_submission(thread)
        comments = tn.fetch_comments(get_id)
        count += len(comments)
    return count


class SideThread:
    """A side thread class, which consists of a validation part and an update
    part In addition to checking whether a collection of counts is valid
    according to the side thread rule, the class can take a mapping
    comment->count and using this try and identify when errors were made in the
    chain. The class will also attempt to determine how many total counts have
    been made in a given side thread using one of:

    - The comment->count mapping to determine the current count, which is then
    applied to the submission title

    - The update_function parameter, which takes in the current state and
    returns the total number of counts. Sensible approaches for doing this are
    either parsing the current state from the title if it's different from the
    comment->count mapping, or traversing the chain of comments until the last
    known state is reached, and adding on all the comments encountered along
    the way. This is useful for threads which don't have a constant number of
    counts between gets, e.g. tug of war.

    - A standard thread length

    The approaches are listed in low->high priority, so if more than one
    approach is supplied the highest priority one is used.

    """

    def __init__(
        self,
        rule=default_rule,
        form=permissive,
        length=None,
        comment_to_count=None,
        update_function=None,
    ):
        self.form = form
        self.rule = rule
        self.history = None
        self.comment_to_count = None
        if comment_to_count is not None:
            self.comment_to_count = comment_to_count
            self.update_count = make_title_updater(comment_to_count)
        if update_function is not None:
            self.update_count = update_function
        if length is not None or (comment_to_count is None and update_function is None):
            self.length = length if length is not None else 1000
            self.update_count = self.update_from_length

    def update_from_length(self, old_count, chain, was_revival=None):
        chain = ignore_revivals(chain, was_revival)[1:]
        if self.length is not None:
            return old_count + self.length * (len(chain))
        return None

    def is_valid_thread(self, history):
        mask = self.rule.is_valid(history)
        if mask.all():
            return (True, "")
        return (False, history.loc[~mask, "comment_id"].iloc[0])

    def is_valid_count(self, comment, history):
        history = pd.concat([history, pd.DataFrame([comment_to_dict(comment)])], ignore_index=True)
        valid_history = self.is_valid_thread(history)[0]
        valid_count = self.looks_like_count(comment)
        valid_user = not counters.is_ignored_counter(str(comment.author))
        return valid_history and valid_count and valid_user, history

    def get_history(self, comment):
        """Fetch enough previous comments to be able to determine whether replies to
        `comment` are valid according to the side thread rules.
        """
        return self.rule.get_history(comment)

    def looks_like_count(self, comment):
        return comment.body in utils.deleted_phrases or self.form(comment.body)

    def set_comment_to_count(self, f):
        self.comment_to_count = f

    def wrapped_comment_to_count(self, comment):
        comment_to_count = (
            self.comment_to_count if self.comment_to_count is not None else base_n_count(10)
        )
        try:
            return comment_to_count(comment)
        except ValueError:
            return np.nan

    def find_errors(self, history):
        """Find points in the history of a side thread where an incorrect count was posted.

        Parameters:
          - history: Either a string representing the comment id of
            the leaf comment in the thread to be investigated, or a pandas
            dataframe with (at least) a "body" column that contains the markdown
            string of each comment in the thread.

        Returns:
          - The comments in the history where an uncorrected error was introduced

        In order to do this, we need to use the `comment_to_count` member of
        the side thread to go from the string representation of a comment to
        the corresponding count. This is potentially different for each side
        thread.

        Errors are defined narrowly to avoid having too many false positives. A
        comment is considered to introduce an error if:

          - Its count is not one more than the previous count AND
          - Its count is not two more than the last but one count AND
          - Its count doesn't match where the count should be according to the
            position in the thread.

        The last criterion means that counts which correct previous errrors won't be included.

        Additionally, to avoid rehashing errors which have already been
        corrected, only comments after the last correct count in the thread
        will be considered.

        """
        if isinstance(history, str):
            self.history = pd.DataFrame(tn.fetch_comments(history))
            history = self.history

        counts = history["body"].apply(self.wrapped_comment_to_count)
        # Errors are points where the count doesn't match the index difference
        errors = counts - counts.iloc[0] != counts.index
        # But only errors after the last correct value are interesting
        errors[: errors.where((~errors)).last_valid_index()] = False
        mask = errors & (counts.diff() != 1) & (counts.diff(2) != 2)
        return history[mask]


class OnlyRepeatingDigits(SideThread):
    """
    A class that describes the only repeating digits side thread.

    The rule and form attributes of the side thread are the same as for base n;
    no validation that each digit actually occurs twice is currently done.

    The main aspect of this class is the update function, which is mathematically
    quite heavy. To count the number of possible only repeating digits strings,
    we build a transition matrix where the states for each digits are
      - Seen 0 times
      - Seen 1 time
      - Seen 2 or more times
    The success states are 0 and 2.

    For strings of length n, we count the success states by looking at the
    nth power of the transition matrix.
    """

    def __init__(self, n=10, rule=default_rule):
        form = base_n(n)
        self.n = n
        self.lookup = {"0": "1", "1": "2", "2": "2"}
        self.indices = self._indices(self.n)
        self.transition_matrix = self.make_dfa()
        super().__init__(rule=rule, form=form, comment_to_count=self.count)

    def connections(self, i):
        base3 = np.base_repr(i, 3).zfill(self.n)
        js = [int(base3[:ix] + self.lookup[x] + base3[ix + 1 :], 3) for ix, x in enumerate(base3)]
        result = collections.defaultdict(int)
        for j in js:
            if j == 1:
                continue
            result[j] += 1
        return (
            len(result),
            np.array(list(result.keys()), dtype=int),
            np.array(list(result.values()), dtype=int),
        )

    def make_dfa(self):
        data = np.zeros(self.n * 3**self.n, dtype=int)
        x = np.zeros(self.n * 3**self.n, dtype=int)
        y = np.zeros(self.n * 3**self.n, dtype=int)
        ix = 0
        for i in range(3**self.n):
            length, js, new_data = self.connections(i)
            x[ix : ix + length] = i
            y[ix : ix + length] = js
            data[ix : ix + length] = new_data
            ix += length
        return scipy.sparse.coo_matrix(
            (data[:ix], (x[:ix], y[:ix])), shape=(3**self.n, 3**self.n)
        )

    def _indices(self, n):
        if n == 0:
            return [0]
        partial = [3 * x for x in self._indices(n - 1)]
        return sorted(partial + [x + 2 for x in partial])

    def count_only_repeating_words(self, k):
        """The number of words of length k where no digit is present exactly once"""
        # The idea is to use the inclusion-exclusion principle, starting with
        # all n ^ k possible words. We then subtract all words where a given
        # symbol occurrs only once. For each symbol there are k * (n-1) ^ (k-1)
        # such words since there are k slots for the symbol of interest, and
        # the remaining slots must be filled with one of the remaining symbols.
        # There are thus n * k * (n-1)^ *(k-1) words where one symbol occurs
        # only once. But this double counts all the cases where two symbols
        # occur only once, so we have to add them back in. In general, there
        # are (n-i)^(n-i) * C(n,i) * P(k,i) words where i symbols occur only
        # once, giving the expression:

        # The correction factor (n-1)/n accounts for the words which would
        # start with a 0

        return (
            sum(
                (-1) ** i * (self.n - i) ** (k - i) * math.comb(self.n, i) * math.perm(k, i)
                for i in range(0, min(self.n, k) + 1)
            )
            * (self.n - 1)
            // self.n
        )

    def get_state(self, prefix):
        result = ["0"] * self.n
        for char in prefix:
            index = (self.n - 1) - int(char, self.n)
            result[index] = self.lookup[result[index]]
        return int("".join(result), 3)

    def count(self, comment):
        count = parsing.find_count_in_text(comment)
        word = str(count)
        word_length = len(word)
        if word_length < 2:
            return 0
        result = sum(self.count_only_repeating_words(i) for i in range(1, word_length))
        result += (
            (int(word[0], self.n) - 1)
            * self.count_only_repeating_words(word_length)
            // (self.n - 1)
        )
        current_matrix = scipy.sparse.identity(3**self.n, dtype="int", format="csr")
        for i in range(word_length - 1, 0, -1):
            prefix = word[:i]
            current_char = word[i].upper()
            suffixes = alphanumeric[: string.digits.index(current_char)]
            states = [self.get_state(prefix + suffix) for suffix in suffixes]
            result += sum(current_matrix[state, self.indices].sum() for state in states)
            current_matrix *= self.transition_matrix
        return result


known_threads = {
    "-illion": SideThread(form=illion_form, length=1000),
    "2d20 experimental v theoretical": SideThread(form=d20_form, length=1000),
    "balanced ternary": SideThread(form=balanced_ternary, length=729),
    "base 16 roman": SideThread(form=roman_numeral),
    "base 2i": SideThread(form=base_n(4), comment_to_count=gaussian_integer_count),
    "bijective base 2": SideThread(form=base_n(3), length=1024),
    "binary encoded decimal": SideThread(
        form=base_n(2), comment_to_count=binary_coded_decimal_count
    ),
    "binary encoded hexadecimal": SideThread(form=base_n(2), length=1024),
    "by 3s in base 7": SideThread(form=base_n(7)),
    "by 3s": SideThread(comment_to_count=by_ns_count(3)),
    "by 4s": SideThread(comment_to_count=by_ns_count(4)),
    "by 5s": SideThread(comment_to_count=by_ns_count(5)),
    "by 7s": SideThread(comment_to_count=by_ns_count(7)),
    "by 99s": SideThread(comment_to_count=by_ns_count(99)),
    "collatz conjecture": SideThread(comment_to_count=collatz_count, form=base_10),
    "colored squares": SideThread(form=colored_squares_form, length=729),
    "cyclical bases": SideThread(form=base_n(16)),
    "dates": SideThread(form=base_10, update_function=update_dates),
    "decimal encoded sexagesimal": SideThread(length=900, form=base_10),
    "dollars and cents": SideThread(form=base_n(4)),
    "double increasing": SideThread(form=base_10, comment_to_count=increasing_type_count(2)),
    "fast or slow": SideThread(rule=FastOrSlow()),
    "four fours": SideThread(form=validate_from_character_list("4")),
    "increasing sequences": SideThread(form=base_10, comment_to_count=increasing_type_count(1)),
    "invisible numbers": SideThread(form=base_n(10, strip_links=False)),
    "japanese": SideThread(form=validate_from_character_list("一二三四五六七八九十百千")),
    "mayan numerals": SideThread(length=800, form=mayan_form),
    "no repeating digits": SideThread(comment_to_count=no_repeating_count),
    "no successive digits": SideThread(comment_to_count=no_successive_count, form=base_10),
    "once per thread": SideThread(form=base_10, rule=CountingRule(wait_n=None)),
    "only double counting": SideThread(form=base_10, rule=OnlyDoubleCounting()),
    "only repeating digits": OnlyRepeatingDigits(),
    "parentheses": SideThread(form=parentheses_form),
    "planetary octal": SideThread(length=1024, form=planetary_octal_form),
    "powerball": SideThread(comment_to_count=powerball_count, form=base_10),
    "rainbow": SideThread(length=1029, form=rainbow_form),
    "reddit usernames": SideThread(length=722, form=reddit_username_form),
    "roman progressbar": SideThread(form=roman_numeral),
    "roman": SideThread(form=roman_numeral),
    "slow": SideThread(form=base_10, rule=CountingRule(thread_time=MINUTE)),
    "slower": SideThread(form=base_10, rule=CountingRule(user_time=HOUR)),
    "slowestest": SideThread(form=base_10, rule=CountingRule(thread_time=HOUR, user_time=DAY)),
    "symbols": SideThread(form=validate_from_character_list("!@#$%^&*()")),
    "throwaways": SideThread(form=throwaway_form),
    "triple increasing": SideThread(form=base_10, comment_to_count=increasing_type_count(3)),
    "twitter handles": SideThread(length=1369, form=twitter_form),
    "unary": SideThread(form=validate_from_character_list("|")),
    "unicode": SideThread(form=base_n(16), length=1024),
    "using 12345": SideThread(form=validate_from_character_list("12345")),
    "valid brainfuck programs": SideThread(form=brainfuck),
    "wait 10": SideThread(form=base_10, rule=CountingRule(wait_n=10)),
    "wait 2 - letters": SideThread(rule=CountingRule(wait_n=2)),
    "wait 2": SideThread(form=base_10, rule=CountingRule(wait_n=2)),
    "wait 3": SideThread(form=base_10, rule=CountingRule(wait_n=3)),
    "wait 4": SideThread(form=base_10, rule=CountingRule(wait_n=4)),
    "wait 5s": SideThread(form=base_10, rule=CountingRule(thread_time=5)),
    "wait 9": SideThread(form=base_10, rule=CountingRule(wait_n=9)),
    "wave": SideThread(form=base_10, comment_to_count=wave_count),
}


base_n_threads = {
    f"base {n}": SideThread(form=base_n(n), comment_to_count=base_n_count(n)) for n in range(2, 37)
}
known_threads.update(base_n_threads)

# See: https://www.reddit.com/r/counting/comments/o7ko8r/free_talk_friday_304/h3c7433/?context=3

default_threads = [
    "10 at a time",
    "3 or fewer palindromes",
    "69, 420, or 666",
    "age",
    "all even or all odd",
    "by 0.02s",
    "by 10s",
    "by 123s",
    "by 12s",
    "by 20s",
    "by 23s",
    "by 29s",
    "by 2s even",
    "by 2s odd",
    "by 3s",
    "by 40s",
    "by 4s",
    "by 50s",
    "by 5s",
    "by 64s",
    "by 69s",
    "by 7s",
    "by 8s",
    "by 8s",
    "by 99s",
    "by meters",
    "by one-hundredths",
    "california license plates",
    "decimal",
    "four squares",
    "n read as base n number",
    "negative numbers",
    "no consecutive digits",
    "palindromes",
    "powers of 2",
    "previous dates",
    "prime factorization",
    "prime numbers",
    "rational numbers",
    "rotational symmetry",
    "scientific notation",
    "sheep",
    "street view counting",
    "thread completion",
    "top subreddits",
    "triangular numbers",
    "unordered consecutive digits",
    "william the conqueror",
]
known_threads.update(
    {thread_name: SideThread(form=base_10, length=1000) for thread_name in default_threads}
)

default_threads = {
    "eban": 800,
    "factoradic": 720,
    "feet and inches": 600,
    "hoi4 states": 806,
    "ipv4": 1024,
    "lucas numbers": 200,
    "permutations": 720,
    "seconds minutes hours": 1200,
    "time": 900,
}
known_threads.update(
    {key: SideThread(form=base_10, length=length) for key, length in default_threads.items()}
)


no_validation = {
    "acronyms": 676,
    "base 40": 1600,
    "base 60": 900,
    "base 62": 992,
    "base 64": 1024,
    "base 93": 930,
    "beenary": 1024,
    "cards": 676,
    "degrees": 900,
    "iterate each letter": None,
    "letters": 676,
    "musical notes": 1008,
    "octal letter stack": 1024,
    "palindromes - letters": 676,
    "permutations - letters": None,
    "previous_dates": None,
    "qwerty alphabet": 676,
    "youtube": 1024,
}

known_threads.update({k: SideThread(length=v) for k, v in no_validation.items()})

default_thread_varying_length = [
    "2d tug of war",
    "boost 5",
    "by day of the week",
    "by day of the year",
    "by digits in total karma",
    "by gme increase/decrease",
    "by length of username",
    "by number of post upvotes",
    "by random number (1-1000)",
    "by random number",
    "by timestamp seconds",
    "comment karma",
    "nim",
    "pick from five",
    "post karma",
    "total karma",
    "tug of war",
]

default_thread_unknown_length = [
    "base of previous digit",
    "by list size",
    "by number of digits squared",
    "divisors",
]


def get_side_thread(thread_name):
    """Return the properties of the side thread with first post thread_id"""
    if thread_name in known_threads:
        return known_threads[thread_name]
    if thread_name in default_thread_unknown_length:
        return SideThread(form=base_10)
    if thread_name in default_thread_varying_length:
        return SideThread(update_function=update_from_traversal, form=base_10)
    if thread_name != "default":
        printer.info(
            (
                "No rule found for %s. Not validating comment contents. "
                "Assuming n=1000 and no double counting."
            ),
            thread_name,
        )
    return SideThread()


module_dir = os.path.dirname(__file__)
config = configparser.ConfigParser()
config.read(os.path.join(module_dir, "side_threads.ini"))
known_thread_ids = config["threads"]
