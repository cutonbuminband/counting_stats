import datetime as dt

from rcounting.counters import apply_alias
from rcounting.parsing import parse_markdown_links
from rcounting.reddit_interface import subreddit


def is_within_threshold(post):
    threshold_timestamp = dt.datetime.combine(dt.date.today(), dt.time(hour=7)).timestamp()
    return post.created_utc >= threshold_timestamp


def find_manual_ftf(previous_ftf_poster):
    submissions = []
    for submission in subreddit.new(limit=1000):
        if is_within_threshold(submission):
            submissions.append(submission)
        else:
            break
    candidate_ftfs = [
        submission for submission in submissions if "Free Talk Friday" in submission.title
    ]
    if not candidate_ftfs:
        return False
    if len(candidate_ftfs) > 1:
        for candidate_ftf in candidate_ftfs[::-1]:
            if candidate_ftf.author != previous_ftf_poster:
                return candidate_ftf
    return candidate_ftfs[-1]


def generate_new_title(previous_title):
    n = int(previous_title.split("#")[1])
    return f"Free Talk Friday #{n+1}"


def generate_new_body(previous_ftf_id):

    ftf_body = (
        "Continued from last week's FTF [here](/comments/{}/)\n\n"
        "It's that time of the week again. Speak anything on your mind! "
        "This thread is for talking about anything off-topic, "
        "be it your lives, your strava, your plans, your hobbies, studies, stats, "
        "pets, bears, hikes, dragons, trousers, travels, transit, cycling, family, "
        "or anything you like or dislike, except politics\n\n"
        "Feel free to check out our [tidbits](https://redd.it/n6onl8) thread "
        "and introduce yourself if you haven't already."
    )

    return ftf_body.format(previous_ftf_id)


def make_directory_row(post):
    date = dt.date.fromtimestamp(post.created_utc)
    link = f"[FTF #{post.title.split('#')[1]}](/{post.id})"
    formatted_date = date.strftime("%b %d, %Y")
    author = apply_alias(str(post.author))
    return f"|{link}|{formatted_date}|{author}"


def update_directory(post):
    row = make_directory_row(post)
    wiki = subreddit.wiki["ftf_directory"]
    contents_list = wiki.content_md.split("\n")
    links = [parse_markdown_links(x) for x in contents_list]
    known_posts = [x[0][1][1:] for x in links if x]
    if post.id not in known_posts:
        new_contents = "\n".join(contents_list + [row])
        wiki.edit(new_contents, reason="Added latest FTF")


if __name__ == "__main__":
    previous_ftf_post = subreddit.sticky(number=2)

    if is_within_threshold(previous_ftf_post):
        update_directory(previous_ftf_post)
    else:
        ftf_post = find_manual_ftf(previous_ftf_post.author)
        if not ftf_post:
            title = generate_new_title(previous_ftf_post.title)
            body = generate_new_body(previous_ftf_post.id)
            ftf_post = subreddit.submit(title=title, selftext=body)

        ftf_post.mod.sticky()
        ftf_post.mod.suggested_sort(sort="new")

        update_directory(ftf_post)
