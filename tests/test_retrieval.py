"""Offline tests for local target selection."""
import pathlib
import sys
import types
import unittest

package = types.ModuleType("retrieval_test")
package.__path__ = [str(pathlib.Path(__file__).parents[1] / "custom_components" / "auralis")]
sys.modules[package.__name__] = package
from retrieval_test.retrieval import select_context


def entity(entity_id, name, area="Küche", domain_action="turn_off", **extra):
    return {"id": entity_id, "name": name, "area": area, "state": "on",
            "actions": [{"id": domain_action, "enabled": True}], **extra}


class RetrievalTests(unittest.TestCase):
    def test_negation_never_offers_actions(self):
        catalog = [entity("light.one", "Leselampe 1")]
        for word in ("nicht", "nie", "niemals", "never", "not"):
            with self.subTest(word=word):
                trace = {}
                _, actions, targets, status = select_context(f"Schalte Leselampe 1 {word} aus", catalog, trace=trace)
                self.assertEqual((actions, targets, status, trace["reason"]), ({}, [], "read_only", "read_only_expression"))

    def test_number_words_and_collisions(self):
        catalog = [entity("light.one", "Leselampe 1"), entity("light.two", "Leselampe 2")]
        selected, actions, _, status = select_context("Schalte Leselampe zwei aus", catalog)
        self.assertEqual(([e["id"] for e in selected], list(actions), status), (["light.two"], ["turn_off"], "matched"))
        catalog.append(entity("light.other", "Leselampe zwei"))
        _, actions, _, status = select_context("Schalte Leselampe 2 aus", catalog)
        self.assertEqual((actions, status), ({}, "clarify"))

    def test_numbered_targets_and_lists_never_select_a_partial_match(self):
        catalog = [entity("light.one", "Leselampe 1"), entity("light.plain", "Deckenlampe")]
        for prompt in ("Schalte Leselampe 12 aus", "Schalte Deckenlampe 12 aus",
                       "Schalte Leselampe 1 bis 2 aus", "Schalte Leselampe 1 und Leselampe 3 aus",
                       "Schalte Leselampe eins, zwei aus"):
            with self.subTest(prompt=prompt):
                trace = {}
                _, actions, targets, status = select_context(prompt, catalog, trace=trace)
                self.assertEqual((actions, targets, status), ({}, [], "clarify"))
                self.assertIn(trace["reason"], {"unresolved_target", "unresolved_number_list"})

    def test_number_words_also_match_room_names(self):
        catalog = [entity("light.one", "Deckenlampe", "Büro Zwei"), entity("light.two", "Deckenlampe", "Büro 3")]
        for room in ("Büro Zwei", "Büro 2"):
            with self.subTest(room=room):
                _, actions, targets, status = select_context(f"Schalte das Licht im {room} aus", catalog)
                self.assertEqual((list(actions), targets, status), (["turn_off"], ["light.one"], "matched"))

    def test_general_conversation_does_not_trigger_target_suggestions(self):
        trace = {}
        self.assertEqual(select_context("Hallo Auralis", [entity("light.one", "Leselampe")], trace=trace), ([], {}, [], "no_match"))
        self.assertNotIn("reason", trace)

    def test_suggestions_are_scoped_bounded_and_never_actions(self):
        catalog = [entity(f"light.{i}", f"Leselampe {i}") for i in range(5)]
        catalog += [entity("light.other", "Leselampe 9", "Flur"), entity("fan.other", "Leselampe Ventilator")]
        trace = {}
        selected, actions, targets, status = select_context("Schalte die Lampe am Sofa in der Küche aus", catalog, trace=trace)
        self.assertEqual((actions, targets, status, trace["reason"]), ({}, [], "clarify", "unresolved_target"))
        self.assertLessEqual(len(selected), 3)
        self.assertTrue(selected)
        self.assertTrue(all(e["area"] == "Küche" and e["id"].startswith("light.") for e in selected))
        selected, actions, _, status = select_context("Schalte die Lampe im Garten aus", catalog)
        self.assertEqual((actions, status), ({}, "clarify"))
        self.assertFalse(any(e["area"] == "Küche" for e in selected))

    def test_bulk_and_known_split_name_remain_available(self):
        catalog = [entity("light.one", "Deckenlampe", "Schlafzimmer"), entity("light.two", "Leselampe", "Schlafzimmer")]
        selected, actions, _, status = select_context("Schalte die Decken Lampe im Schlaf Zimmer aus", catalog)
        self.assertEqual(([e["id"] for e in selected], list(actions), status), (["light.one"], ["turn_off"], "matched"))
        _, actions, targets, status = select_context("Schalt das Licht im Schlafzimmer aus", catalog)
        self.assertEqual((list(actions), targets, status), (["turn_off"], ["light.one", "light.two"], "matched"))


if __name__ == "__main__":
    unittest.main()
