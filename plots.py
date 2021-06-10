import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import matplotlib.colors as mcolors
from datetime import timedelta
from cycler import cycler
from pandas.plotting import register_matplotlib_converters
from parsing import find_count_in_text
from analysis import fft_kde

register_matplotlib_converters()

standard_colors = ['#1f77b4', '#ff7f0e', '#2ca02c', '#d62728', '#9467bd',
                   '#8c564b', '#e377c2', '#7f7f7f', '#bcbd22', '#17becf']


def format_x_date_month(ax):
    months = mdates.MonthLocator()  # every month
    quarters = mdates.MonthLocator([1, 4, 7, 10])
    monthFmt = mdates.DateFormatter('%b %Y')
    ax.xaxis.set_major_locator(quarters)
    ax.xaxis.set_major_formatter(monthFmt)
    ax.xaxis.set_minor_locator(months)


def speedrun_histogram(df, n=3):
    bins = np.arange(0, 21)
    df = df.copy()
    df['dt'] = df['timestamp'].diff()
    counters = df.query('dt < 20').groupby('username').mean()['dt'].sort_values().index
    fig, axes = plt.subplots(n, sharex=True, sharey=True)
    for idx, counter in enumerate(counters[:n]):
        ax = axes[idx]
        ax.hist(df.query('username == @counter')['dt'],
                bins=bins, alpha=0.6, label=counter, density=True,
                color=standard_colors[idx],
                edgecolor='k')
        ax.legend()
    ax.set_xlim(0, 11)
    ax.set_xticks([0.5, 5.5, 10.5])
    ax.set_xticklabels(['0', '5', '10'])
    return fig


def time_of_day_histogram(df, ax, n=4):
    bins = np.linspace(0, 24 * 3600, 24 * 12 + 1)
    df = df.copy()
    df['time_of_day'] = df['timestamp'].astype(int) % (24 * 3600)
    top_counters = df['username'].value_counts().index[:n]
    ax.hist(df['time_of_day'], bins=bins, alpha=0.8, label='total', color='C3',
            edgecolor='k')
    for counter in top_counters:
        data = df.query('username==@counter')['time_of_day']
        ax.hist(data, bins=bins, alpha=0.7, label=counter,
                edgecolor='k')
    ax.set_xlim(0, 24 * 3600 + 1)
    hour = 3600
    ax.set_xticks([0 * hour, 3 * hour, 6 * hour, 9 * hour, 12 * hour,
                   15 * hour, 18 * hour, 21 * hour, 24 * hour])
    ax.set_xticklabels(['00:00', '03:00', '06:00', '09:00', '12:00',
                        '15:00', '18:00', '21:00', '00:00'])
    ax.legend()
    ax.set_ylabel("Number of counts per 5 min interval")
    return ax


def time_of_day_kde(df, ax, n=4):
    alpha = 0.8
    nbins = 24 * 60 * 2
    sigma = 0.02
    df = df.copy()
    df['time_of_day'] = df['timestamp'].astype(int) % (24 * 3600)
    counts = df['username'].value_counts()
    top_counters = counts.index[:n]
    x, kde = fft_kde(df['time_of_day'], nbins, kernel='normal_distribution', sigma=sigma)
    kde *= len(df)
    ax.fill_between(x, kde, label='All Counts', color='0.8')
    for idx, counter in enumerate(top_counters):
        data = df.query('username==@counter')['time_of_day']
        x, kde = fft_kde(data, nbins, kernel='normal_distribution', sigma=sigma)
        kde *= counts.loc[counter]
        ax.fill_between(x, kde, color=standard_colors[idx], alpha=alpha)
        ax.plot(x, kde, label=counter, color=standard_colors[idx], lw=2)
    ax.set_xlim(0, 24 * 3600 + 1)
    hour = 3600
    ax.set_xticks([0 * hour, 3 * hour, 6 * hour, 9 * hour, 12 * hour,
                   15 * hour, 18 * hour, 21 * hour, 24 * hour])
    ax.set_xticklabels(['00:00', '03:00', '06:00', '09:00', '12:00',
                        '15:00', '18:00', '21:00', '00:00'])
    ax.set_ylim(bottom=0)
    ax.legend()
    ax.set_xlabel("Time of day [UTC]")
    ax.set_ylabel("Counts per second")
    return ax


def plot_get_time(df, ax, **kwargs):
    cc = cycler(color=['C0', 'C1']) * cycler(marker=list('vs'))
    ax.set_prop_cycle(cc)
    modstring = {False: "Non-mod", True: "Mod"}
    get_type = {"get": "Get", "assist": "Assist"}

    for count_type in ['get', 'assist']:
        for modness in [False, True]:
            subset = df.query('is_moderator == @modness and count_type == @count_type')
            ax.plot(subset['timestamp'], subset['elapsed_time'],
                    linestyle="None",
                    label=f"{get_type[count_type]} by {modstring[modness]}",
                    alpha=0.8,
                    **kwargs)

    one_day = timedelta(1)
    start_date, end_date = assists['timestamp'].min(), gets['timestamp'].max()
    ax.set_xlim(left=start_date - one_day, right=end_date + one_day)
    format_x_date_month(ax)
    ax.set_ylim(0, 30)
    ax.set_ylabel('Elapsed time for assists and gets [s]')
    ax.legend(loc='upper right')
    return ax


def simulate_alpha(color, alpha):
    white = np.array((1, 1, 1))
    return tuple(np.array(mcolors.to_rgb(color)) * alpha + (1 - alpha) * white)


if __name__ == "__main__":
    gets = pd.read_csv('gets.csv')
    assists = pd.read_csv('assists.csv')

    gets['timestamp'] = pd.to_datetime(gets['timestamp'])
    gets['count_type'] = 'get'
    gets['count'] = gets['body'].apply(find_count_in_text)
    gets = gets.set_index('comment_id')
    assists['timestamp'] = pd.to_datetime(assists['timestamp'])
    assists['count_type'] = 'assist'
    assists['count'] = gets['body'].apply(find_count_in_text)
    assists = assists.set_index('comment_id')

    df = pd.concat([gets, assists])

    fig = plt.figure()
    ax = fig.add_subplot(111)
    ax = plot_get_time(df, ax)
    plt.savefig('assists_gets.png')
    plt.show()
