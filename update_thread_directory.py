import datetime
import configparser
from models import Tree
from side_threads import get_side_thread
from parsing import find_urls_in_text, find_urls_in_submission, find_count_in_text
from parsing import parse_markdown_links, parse_directory_page
from thread_navigation import fetch_comment_tree, walk_down_thread
from utils import flatten

config = configparser.ConfigParser()
config.read('side_threads.ini')
known_threads = config['threads']


class Table():
    def __init__(self, rows):
        self.rows = rows
        for row in self.rows:
            row.archived = False

    def __str__(self):
        table_header = ['Name &amp; Initial Thread|Current Thread|# of Counts',
                        ':--:|:--:|--:']
        return "\n".join(table_header + [str(x) for x in self.rows if not x.archived])

    def update(self, tree):
        for row in self.rows:
            if verbosity > 0:
                print(f"Updating side thread: {row.thread_type}")
            row.update(tree)

    def archived_threads(self):
        rows = [row.copy() for row in self.rows if row.archived]
        return Table(rows)

    def __getitem__(self, key):
        return self.rows[key]

    def __len__(self):
        return len(self.rows)

    @property
    def submissions(self):
        return [row.submission for row in self.rows]

    def sort(self, **kwargs):
        self.rows = sorted(self.rows, **kwargs)


class Row():
    def __init__(self, first_thread, current_thread, current_count):
        thread_name, first_thread_id = parse_markdown_links(first_thread)[0]
        self.thread_name = thread_name.strip()
        self.first_thread = first_thread_id.strip()[1:]
        submission_id, comment_id = find_urls_in_text(current_thread)[0]
        self.submission_id = submission_id
        self.comment_id = comment_id
        self.count_string = current_count.strip()
        self.count = find_count_in_text(self.count_string.replace("-", "0"))
        self.is_approximate = self.count_string[0] == "~"
        self.thread_type = known_threads.get(self.first_thread, fallback='decimal')
        self.side_thread = get_side_thread(self.thread_type)

    def __str__(self):
        return (f"[{self.thread_name}](/{self.first_thread}) | "
                f"[{self.title}]({self.link}) | {self.count_string}")

    def __lt__(self, other):
        return (self.count < other.count
                or (other.is_approximate and not self.is_approximate))

    @property
    def link(self):
        return f"/comments/{self.submission.id}/_/{self.comment_id}?context=3"

    @property
    def title(self):
        sections = self.submission.title.split("|")
        if len(sections) > 1:
            sections = sections[1:]
        title = (' '.join(sections)).strip()
        return title if title else self.count

    def format_count(self):
        if self.count is None:
            return self.count_string + "*"
        if self.count == 0:
            return "-"
        if self.is_approximate:
            return f"~{self.count:,d}"
        return f"{self.count:,d}"

    def update(self, submission_tree):
        submission = tree.node(self.submission_id)
        comment, chain, archived = submission_tree.find_latest_comment(self.side_thread,
                                                                       submission,
                                                                       self.comment_id)
        self.comment_id = comment.id
        self.submission = chain[-1]
        self.archived = archived
        if len(chain) > 1:
            self.count = self.side_thread.update_count(self.count, chain)
            self.count_string = self.format_count()


class SubmissionTree(Tree):
    def __init__(self, submissions, submission_tree, reddit=None, verbosity=1, is_accurate=True):
        self.verbosity = verbosity
        self.is_accurate = is_accurate
        self.reddit = reddit
        super().__init__(submissions, submission_tree)

    def find_latest_comment(self, side_thread, old_submission, comment_id=None):
        chain = self.traverse(old_submission)
        archived = False
        if chain is None:
            archived = True
            chain = [old_submission]
        if len(chain) > 1 or comment_id is None:
            comment_id = chain[-1].comments[0].id
        comments = fetch_comment_tree(chain[-1], root_id=comment_id, verbose=False)
        comments.set_accuracy(self.is_accurate)
        comments.verbose = (self.verbosity > 1)
        new_comment = walk_down_thread(side_thread, comments.comment(comment_id))
        return new_comment, chain, archived

    def node(self, node_id):
        try:
            return super().node(node_id)
        except KeyError:
            if self.reddit is not None:
                return self.reddit.submission(node_id)
        raise


def get_counting_history(subreddit, time_limit, verbosity=1):
    now = datetime.datetime.utcnow()
    submissions = subreddit.new(limit=1000)
    tree = {}
    submissions_dict = {}
    new_threads = []
    for count, submission in enumerate(submissions):
        if verbosity > 1 and count % 20 == 0:
            print(f"Processing reddit submission {submission.id}")
        submissions_dict[submission.id] = submission
        title = submission.title.lower()
        if "tidbits" in title or "free talk friday" in title:
            continue
        try:
            url = next(filter(lambda x: int(x[0], 36) < int(submission.id, 36),
                              find_urls_in_submission(submission)))
            tree[submission.id] = url[0]
        except StopIteration:
            new_threads.append(submission)
        post_time = datetime.datetime.utcfromtimestamp(submission.created_utc)
        if now - post_time > time_limit:
            break
    else:  # no break
        print('Threads between {now - six_months} and {post_time} have not been collected')

    tree = {v: k for k, v in tree.items()}
    return submissions_dict, tree, new_threads


if __name__ == "__main__":
    import argparse
    from reddit_interface import reddit
    parser = argparse.ArgumentParser(description='Update the thread directory located at'
                                     ' reddit.com/r/counting/wiki/directory')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--verbose', '-v', action='store_true',
                       help='Print more output during directory updates')

    group.add_argument('--quiet', '-q', action='store_true',
                       help='Print less output during directory updates')

    parser.add_argument('--fast', '-f', action='store_true',
                        help=('Only use an online archive to fetch the comments '
                              'and not the reddit api. Warning: up to three days out of date!'))

    args = parser.parse_args()
    verbosity = 1 - args.quiet + args.verbose
    is_accurate = not args.fast
    if is_accurate:
        verbosity = 2
    start = datetime.datetime.now()
    subreddit = reddit.subreddit('counting')

    directory_page = subreddit.wiki['directory'].content_md
    directory_page = directory_page.replace("\r\n", "\n")
    document = parse_directory_page(directory_page)

    time_limit = datetime.timedelta(weeks=26.5)
    if verbosity > 0:
        print("Getting history")
    submissions, submission_tree, new_threads = get_counting_history(subreddit,
                                                                     time_limit,
                                                                     verbosity)
    tree = SubmissionTree(submissions, submission_tree, reddit, verbosity, is_accurate)

    if verbosity > 0:
        print("Updating tables")
    updated_document = []
    table_counter = 0
    for paragraph in document:
        if paragraph[0] == "text":
            updated_document.append(paragraph[1])
        elif paragraph[0] == "table":
            table_counter += 1
            table = Table([Row(*x) for x in paragraph[1]])
            table.update(tree)
            if table_counter == 2:
                table.sort(reverse=True)
            updated_document.append(table)

    with open("directory.md", "w") as f:
        print(*updated_document, file=f, sep='\n\n')

    table = Table(flatten([x.rows for x in updated_document if hasattr(x, 'rows')]))
    archived_threads = table.archived_threads()
    if archived_threads:
        n = len(archived_threads)
        print(f'writing {n} archived thread{"s" if n > 1 else ""} to archived_threads.md')
        with open("archived_threads.md", "w") as f:
            print(archived_threads, file=f)

    new_threads = set(x.id for x in tree.roots)
    known_submissions = set([x.id for x in table.submissions])
    new_threads = new_threads - known_submissions
    if new_threads:
        with open("new_threads.txt", "w") as f:
            if verbosity > 1:
                n = len(new_threads)
                print(f"{n} new thread{'' if n == 1 else 's'} found. Writing to file")
            print(*[f"New thread '{reddit.submission(x).title}' "
                    f"at reddit.com/comments/{x}" for x in new_threads],
                  sep="\n", file=f)

    end = datetime.datetime.now()
    print(end - start)
