import datetime as dt

from fantasy.lineup import lineup_fit
from fantasy.players import Player
from fantasy.schedule import Schedule
from fantasy.waivers import drop_pool

MON = dt.date(2026, 11, 16)


def _p(pid, name, pos, team, value, **kw):
    p = Player(pid=pid, name=name, positions=pos, team=team, **kw)
    p.blended_value = value
    return p


def test_drop_pool_prefers_streamers_and_never_protected():
    keeper = _p(1, "keeper", ["PG"], "AAA", -2.0, protected=True)
    bailey = _p(2, "bailey", ["SF"], "AAA", -1.0)
    ware = _p(3, "ware", ["C"], "AAA", -0.5, streamer=True)
    assert drop_pool([keeper, bailey, ware]) == [ware]
    # No streamers named: anyone unprotected, still never the keeper.
    ware.streamer = False
    assert drop_pool([keeper, bailey, ware]) == [bailey, ware]


def test_fit_counts_only_games_he_would_start():
    # One C slot. A better center plays both days; the pickup is only useful
    # on the day the starter rests.
    starter = _p(1, "starter", ["C"], "AAA", 3.0)
    pickup = _p(2, "pickup", ["C"], "BBB", 0.5)
    tue = MON + dt.timedelta(days=1)
    schedule = Schedule(by_date={MON.isoformat(): {"AAA", "BBB"}, tue.isoformat(): {"BBB"}})
    fit = lineup_fit(pickup, [starter, pickup], ["C"], schedule, [MON, tue])
    assert (fit.starts, fit.games) == (1, 2)
    assert fit.benched_days == [MON]
