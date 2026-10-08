"""
End-to-end tests over the real Jolpica dataset: need `python -m seed.seed_all` run
and MongoDB + Neo4j running.   Run:  .venv/bin/python -m unittest -v
"""
import unittest

from integration import mediator


def ids(doc, entity):
    return [e.get("id") for e in doc.iterfind(f"{entity}s/{entity}")]


class IntegrationTest(unittest.TestCase):
    def test_driver_merged_from_all_three_sources(self):
        driver = mediator.integrate("Hamilton").find("Drivers/Driver[@id='hamilton']")
        self.assertEqual(driver.find("Profile").get("source"), "rdbms")
        self.assertEqual(driver.findtext("Profile/world_titles"), "7")
        self.assertGreater(int(driver.findtext("Laps/laps_logged")), 20000)       # MongoDB aggregation
        teams = [t.text for t in driver.iterfind("Relations/teams/team")]          # Neo4j DROVE_FOR
        self.assertEqual(teams[:2], ["McLaren", "Mercedes"])

    def test_team_has_car_spec_and_engine_partners(self):
        team = mediator.integrate("Ferrari").find("Teams/Team[@id='ferrari']")
        self.assertEqual(team.findtext("Car/car_name"), "SF-24")
        partners = {t.text for t in team.iterfind("Relations/engine_partners/team")}
        self.assertIn("Haas F1 Team", partners)

    def test_circuit_lap_record_comes_from_mongodb(self):
        circuit = mediator.integrate("Monaco").find("Circuits/Circuit[@id='monaco']")
        record = circuit.find("Laps/fastest_race_lap")
        self.assertEqual((record.text, record.get("driver"), record.get("season")),
                         ("1:12.909", "Lewis Hamilton", "2021"))

    def test_car_name_resolved_by_mongodb(self):
        doc = mediator.integrate("MCL38")
        self.assertEqual(ids(doc, "Team"), ["mclaren"])
        self.assertIn("mongodb", doc.find("Teams/Team").get("matchedBy"))

    def test_engine_resolved_by_neo4j(self):
        doc = mediator.integrate("Honda RBPT")
        self.assertEqual(set(ids(doc, "Team")), {"red_bull", "rb"})

    def test_accent_insensitive_and_exact_code_first(self):
        self.assertEqual(ids(mediator.integrate("Perez"), "Driver")[0], "perez")
        self.assertEqual(ids(mediator.integrate("VER"), "Driver")[0], "max_verstappen")

    def test_name_matches_ranked_by_wins_across_eras(self):
        # regression: drivers without a 3-letter code (pre-2000s) must not sink to the bottom
        self.assertEqual(ids(mediator.integrate("Brazilian"), "Driver")[:2], ["senna", "piquet"])

    def test_results_are_capped_per_entity(self):
        self.assertEqual(len(ids(mediator.integrate("British"), "Driver")), mediator.LIMIT)

    def test_all_three_sources_appear_in_trace(self):
        sources = {q.get("source") for q in mediator.integrate("Senna").iterfind("Trace/SourceQuery")}
        self.assertEqual(sources, {"rdbms", "mongodb", "neo4j"})

    def test_unknown_term_returns_valid_empty_document(self):
        self.assertEqual(mediator.integrate("zzzz-no-match").get("entities"), "0")

    def test_injection_attempts_are_treated_as_plain_text(self):
        for term in ["' OR 1=1 --", ".*", "') RETURN 1 //", "%"]:
            doc = mediator.integrate(term)
            self.assertLessEqual(int(doc.get("entities")), 3 * mediator.LIMIT, term)
        self.assertEqual(mediator.integrate("' OR 1=1 --").get("entities"), "0")

    def test_xpath_across_sources(self):
        doc = mediator.integrate("Monaco")
        self.assertEqual(mediator.run_xpath(doc, "string(//Circuit/Relations/top_winners/driver[1])"), ["Ayrton Senna"])


if __name__ == "__main__":
    unittest.main()
