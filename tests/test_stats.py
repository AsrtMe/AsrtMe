import importlib.util
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

MODULE = Path(__file__).resolve().parents[1] / "scripts" / "update_stats.py"
spec = importlib.util.spec_from_file_location("grimoire_stats", MODULE)
stats = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stats)


def fixture(now=datetime(2026, 11, 19, 12, tzinfo=timezone.utc)):
    start, end = stats.period(now)
    days = [{"date": d.isoformat(), "count": 0} for d in stats.days_between(start, end)]
    days[0]["count"] = 1
    days[-1]["count"] = 6
    return stats.make_snapshot("AsrtMe", start, end, days, 7, 1, 2,
                               {"kind": "test", "checked_on": end.isoformat()})


class PeriodAndCalendarTests(unittest.TestCase):
    def test_365_days_and_leap_year_alignment(self):
        for current in [date(2024, 2, 29), date(2026, 11, 19), date(2026, 12, 31), date(2027, 1, 1)]:
            start, end = stats.period(datetime.combine(current, datetime.min.time(), timezone.utc))
            self.assertEqual(len(stats.days_between(start, end)), 365)
            origin, cells = stats.calendar_layout(start, end)
            self.assertEqual(origin.weekday(), 0)
            self.assertEqual(len({c["date"] for c in cells}), 365)
            for cell in cells:
                self.assertEqual(origin + timedelta(days=cell["column"]*7+cell["row"]), cell["date"])
            self.assertLessEqual(max(c["column"] for c in cells), 52)

    def test_calendar_rejects_missing_duplicate_and_mismatched_counts(self):
        data = fixture()
        start, end = date.fromisoformat(data["period"]["start"]), date.fromisoformat(data["period"]["end"])
        with self.assertRaises(stats.StatsError):
            stats.validate_calendar(data["calendar"][:-1], start, end)
        with self.assertRaises(stats.StatsError):
            stats.validate_calendar(data["calendar"] + [data["calendar"][0]], start, end)
        with self.assertRaises(stats.StatsError):
            stats.validate_calendar(data["calendar"], start, end, 99)

    def test_public_tooltips_use_counts_not_color_levels(self):
        parser = stats.PublicCalendarParser()
        parser.feed('<td id="one" data-date="2026-11-19" data-level="4"></td>'
                    '<tool-tip for="one">7 contributions on November 19th.</tool-tip>')
        self.assertEqual(parser.days(), [{"date": "2026-11-19", "count": 7}])
        missing = stats.PublicCalendarParser()
        missing.feed('<td id="one" data-date="2026-11-19" data-level="4"></td>')
        with self.assertRaises(stats.StatsError):
            missing.days()


class GraphQLTests(unittest.TestCase):
    @staticmethod
    def repo(name, stars, private=False, fork=False, owner="AsrtMe"):
        return {"nameWithOwner": f"{owner}/{name}", "owner": {"login": owner},
                "isPrivate": private, "isFork": fork, "stargazerCount": stars}

    def test_all_repository_pages_and_filtering(self):
        data = fixture()
        calendar = {"login": "AsrtMe", "contributionsCollection": {"contributionCalendar": {
            "totalContributions": 7, "weeks": [{"contributionDays": [
                {"date": d["date"], "contributionCount": d["count"]} for d in data["calendar"]]}]}}}
        pages = [
            {"login": "AsrtMe", "repositories": {"nodes": [self.repo("first", 2), self.repo("AsrtMe", 50), self.repo("fork", 10, fork=True)], "pageInfo": {"hasNextPage": True, "endCursor": "next"}}},
            {"login": "AsrtMe", "repositories": {"nodes": [self.repo("second", 4), self.repo("secret", 100, private=True), self.repo("foreign", 3, owner="Other")], "pageInfo": {"hasNextPage": False, "endCursor": None}}},
        ]
        with patch.object(stats, "graphql", side_effect=[calendar]+pages) as mocked:
            got = stats.fetch_graphql_snapshot("AsrtMe", datetime(2026,11,19,12,tzinfo=timezone.utc), "test-token")
        self.assertEqual(mocked.call_count, 3)
        self.assertEqual(mocked.call_args_list[2].args[1]["cursor"], "next")
        self.assertEqual(got["counts"], {"contributions":7,"public_projects":2,"stars":6})
        self.assertNotIn("secret", json.dumps(got))

    def test_graphql_errors_are_not_treated_as_zeros(self):
        with patch.object(stats, "fetch_json", return_value={"data": {"user": None}, "errors": [{"message":"bad"}]}):
            with self.assertRaises(stats.StatsError):
                stats.graphql("query", {"login":"AsrtMe"}, "test-token")


class RenderAndWriteTests(unittest.TestCase):
    def test_unicode_and_xml_escaping(self):
        label = stats.svg_text('Руны < & " >', 10, 20)
        root = ET.fromstring('<svg xmlns="http://www.w3.org/2000/svg">'+label+'</svg>')
        self.assertEqual(root[0].text, 'Руны < & " >')
        for render in [stats.render_stats, stats.render_stats_mobile, stats.render_activity, stats.render_activity_mobile]:
            ET.fromstring(render(fixture()))

    def test_no_write_on_bad_data_and_no_churn_on_same_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            data = fixture()
            self.assertEqual(len(stats.write_outputs(data, output)), 5)
            before = {p.name: p.read_bytes() for p in output.iterdir()}
            self.assertEqual(stats.write_outputs(data, output), [])
            data["counts"]["contributions"] = 100
            with self.assertRaises(stats.StatsError):
                stats.write_outputs(data, output)
            self.assertEqual(before, {p.name:p.read_bytes() for p in output.iterdir()})

    def test_unknown_calendar_has_no_invented_cells(self):
        data = fixture()
        data.update(calendar_status="unavailable", calendar=[])
        data["counts"]["contributions"] = None
        stats.validate_snapshot(data)
        svg = stats.render_activity(data)
        self.assertIn("Печать ждёт данных", svg)
        self.assertNotIn("01.01.2026:", svg)


if __name__ == "__main__":
    unittest.main()
