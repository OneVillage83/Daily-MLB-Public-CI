from __future__ import annotations

import requests

HEADERS = {
    "User-Agent": "Daily-MLB-GameState-Probe/1.0",
    "Accept": "application/json",
    "Accept-Encoding": "identity",
}


def get_json(url: str, *, params: dict[str, object] | None = None) -> dict[str, object]:
    response = requests.get(
        url,
        params=params,
        headers=HEADERS,
        timeout=30,
        allow_redirects=False,
    )
    print("HTTP", response.status_code, response.url)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise TypeError("probe response root must be an object")
    return payload


def main() -> None:
    schedule = get_json(
        "https://statsapi.mlb.com/api/v1/schedule",
        params={"sportId": 1, "date": "2026-07-26"},
    )
    dates = schedule.get("dates", [])
    assert isinstance(dates, list)
    games = [game for date in dates for game in date.get("games", [])]
    assert games, "schedule probe returned no games"
    game_pk = games[0]["gamePk"]
    feed = get_json(f"https://statsapi.mlb.com/api/v1.1/game/{game_pk}/feed/live")

    game_data = feed.get("gameData", {})
    live_data = feed.get("liveData", {})
    assert isinstance(game_data, dict)
    assert isinstance(live_data, dict)
    boxscore = live_data.get("boxscore", {})
    assert isinstance(boxscore, dict)
    teams = boxscore.get("teams", {})
    assert isinstance(teams, dict)

    print("PROBE_GAME_PK", game_pk)
    print("TOP_LEVEL_KEYS", sorted(feed.keys()))
    print("GAME_DATA_KEYS", sorted(game_data.keys()))
    print("LIVE_DATA_KEYS", sorted(live_data.keys()))
    status = game_data.get("status", {})
    probable = game_data.get("probablePitchers", {})
    players_data = game_data.get("players", {})
    assert isinstance(status, dict)
    assert isinstance(probable, dict)
    assert isinstance(players_data, dict)
    print("STATUS_KEYS", sorted(status.keys()))
    print("STATUS", status)
    print("PROBABLE_PITCHER_SIDES", sorted(probable.keys()))
    print("PROBABLE_PITCHERS", probable)
    print("GAME_DATA_PLAYER_COUNT", len(players_data))
    print("BOXSCORE_KEYS", sorted(boxscore.keys()))

    for side in ("away", "home"):
        team = teams.get(side, {})
        assert isinstance(team, dict)
        print(f"{side.upper()}_TEAM_KEYS", sorted(team.keys()))
        for bucket in ("batters", "pitchers", "bench", "bullpen"):
            values = team.get(bucket, [])
            assert isinstance(values, list)
            print(f"{side.upper()}_{bucket.upper()}_COUNT", len(values))
            print(f"{side.upper()}_{bucket.upper()}_IDS", values)
        order = team.get("battingOrder", [])
        assert isinstance(order, list)
        print(f"{side.upper()}_BATTING_ORDER_COUNT", len(order))
        print(f"{side.upper()}_BATTING_ORDER", order)
        players = team.get("players", {})
        assert isinstance(players, dict)
        print(f"{side.upper()}_BOXSCORE_PLAYER_COUNT", len(players))
        if order:
            key = f"ID{order[0]}"
            player = players.get(key, players_data.get(key, {}))
            assert isinstance(player, dict)
            person = player.get("person", {})
            position = player.get("position", {})
            game_status = player.get("gameStatus", {})
            assert isinstance(person, dict)
            assert isinstance(position, dict)
            assert isinstance(game_status, dict)
            print(f"{side.upper()}_FIRST_ORDER_PLAYER_KEY", key)
            print(f"{side.upper()}_FIRST_ORDER_PLAYER_KEYS", sorted(player.keys()))
            print(f"{side.upper()}_FIRST_ORDER_PERSON", person)
            print(f"{side.upper()}_FIRST_ORDER_POSITION", position)
            print(f"{side.upper()}_FIRST_ORDER_GAME_STATUS", game_status)

    print("SOURCE_PROBE_OK")


if __name__ == "__main__":
    main()
