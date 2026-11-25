"""Draft labelled names and name pairs for a person to confirm (Step 18.1). Not shipped.

    uv run python eval/tools/er_candidates.py draft [--snapshot real] [--out var/er_candidates]
    uv run python eval/tools/er_candidates.py commit --by NAME [--out var/er_candidates]

`draft` reads a built set (read-only) and writes `labelled.csv` and `pairs.csv`
with an `evidence` column (why the identity is believed) and an empty
`confirm` column. Nothing goes into `eval/` until a person has marked rows
`y` and run `commit`, which appends those rows to `eval/er_labelled_real.csv`
and `eval/er_pairs_real.csv` with `labelled_by` and the set id, skipping rows
already there. A pair whose `same` is empty must be filled in before commit.

Where the identity comes from:
- an OpenSanctions record that states an LEI is joined to GLEIF's entity, which
  keeps only GLEIF's names: the list's names of it are queries the graph does
  not hold, and the LEI states whom they name;
- an OpenSanctions entity that states its names in several scripts;
- a variant written from a real name by a rule (spacing, initials, a
  given-name variant, a generic suffix, a legal form written out), which names
  what the name names;
- near-homonyms: one letter of an acronym changed, to a name nobody holds
  (a query that must find nothing) or to another entity's name (a pair of two);
- GLEIF records of different LEIs sharing an authority and number: `same` is
  left for the person, since a fund and its umbrella share a number too.

Sampling is by an md5 of the id and `--seed`, so a draft is the same each time.
"""

import argparse
import csv
import json
import re
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from ubo_sentinel.graph.entity_resolution import PAIR_COLUMNS  # noqa: E402
from ubo_sentinel.graph.store import GraphStore  # noqa: E402
from ubo_sentinel.pipeline.name_match import normalise_name  # noqa: E402

LABELLED = Path("eval/er_labelled_real.csv")
PAIRS = Path("eval/er_pairs_real.csv")
LABELLED_COLUMNS = (
    "query_name", "lei", "jurisdiction", "expected_entity_id", "variant_type",
    "labelled_by", "set_id",
)  # fmt: skip
DRAFT_EXTRA = ("evidence", "confirm")

# The generic words of the plan (19.2), for classifying and writing suffixes.
GENERIC = (
    "group", "holding", "holdings", "company", "international", "trading", "industries",
    "oil", "gas", "bank", "shipping",
)  # fmt: skip
SUFFIXES = ("Group", "Holding", "International", "Trading")
# A short form and the words it stands for, both ways.
LEGAL_FORMS = {
    "LLC": "Limited Liability Company",
    "Ltd": "Limited",
    "LTD": "LIMITED",
    "JSC": "Joint Stock Company",
    "PJSC": "Public Joint Stock Company",
    "Inc": "Incorporated",
    "Corp": "Corporation",
    "GmbH": "Gesellschaft mit beschränkter Haftung",
    "AG": "Aktiengesellschaft",
    "plc": "Public Limited Company",
    "PLC": "PUBLIC LIMITED COMPANY",
}
# Spellings of one given name. Kept here, for drafting only: Step 19 keeps its own.
GIVEN_NAMES = (
    ("Mohammed", "Muhammad", "Mohamed", "Mohammad"),
    ("Ahmed", "Ahmad"),
    ("Hussein", "Husain", "Hussain"),
    ("Yusuf", "Yousef", "Youssef"),
    ("Aleksandr", "Alexander", "Aleksander"),
    ("Sergei", "Sergey"),
    ("Dmitri", "Dmitry", "Dmitriy"),
    ("Yuri", "Yury", "Yuriy"),
    ("Mikhail", "Michail"),
    ("Abdul", "Abdel", "Abd al"),
    ("Omar", "Umar"),
    ("Osama", "Usama"),
)


def script_of(name: str) -> str:
    """The script most of a name's letters are in."""
    counts: dict[str, int] = {}
    for char in name:
        if char.isalpha():
            script = unicodedata.name(char, "OTHER").split(" ")[0]
            script = {"CJK": "HAN", "HIRAGANA": "KANA", "KATAKANA": "KANA"}.get(script, script)
            counts[script] = counts.get(script, 0) + 1
    return max(sorted(counts), key=counts.get) if counts else "NONE"


def classify(query: str, names: list[str]) -> tuple[str, str]:
    """The variant type of a query against the names an entity holds, and the name it is nearest."""
    script = script_of(query)
    same_script = [n for n in names if script_of(n) == script]
    if not same_script:
        return "transliteration", f"{script.lower()} query; the entity's names are in " + ", ".join(
            sorted({script_of(n).lower() for n in names})
        )
    q = normalise_name(query)
    for name in same_script:
        n = normalise_name(name)
        if q.replace(" ", "") == n.replace(" ", ""):
            return "spacing", name
        qt, nt = set(q.split()), set(n.split())
        extra = (qt - nt) | (nt - qt)
        if (qt <= nt or nt <= qt) and extra and extra <= set(GENERIC):
            return "generic_suffix", name
    return "alias", same_script[0]


class Drafter:
    def __init__(self, store: GraphStore, per_type: int, seed: str) -> None:
        self.store, self.per_type, self.seed = store, per_type, seed
        self.labelled: list[dict[str, str]] = []
        self.pairs: list[dict[str, str]] = []
        self.paired: set[tuple[str, str]] = set()

    def rows(self, sql: str, params: list | None = None) -> list[tuple]:
        return self.store.con.execute(sql, [self.store.set_id, *(params or [])]).fetchall()

    def sample(self, where: str, limit: int, columns: str = "id, legal_name, entity_type") -> list:
        return self.rows(
            f"SELECT {columns} FROM graph_nodes WHERE snapshot_set_id = ? AND {where}"
            f" ORDER BY md5(id || '{self.seed}') LIMIT {limit}"
        )

    def holders(self, norms: list[str]) -> dict[str, set[str]]:
        """Normalised name -> the entities holding it."""
        found: dict[str, set[str]] = {}
        for i in range(0, len(norms), 5000):
            for norm, node in self.rows(
                "SELECT DISTINCT name_norm, node_id FROM graph_name_blocks"
                " WHERE snapshot_set_id = ? AND name_norm IN (SELECT unnest(?))",
                [norms[i : i + 5000]],
            ):
                found.setdefault(norm, set()).add(node)
        return found

    def names_of(self, ids: list[str]) -> dict[str, list[str]]:
        names: dict[str, list[str]] = {}
        for entity_id, name in self.rows(
            "SELECT entity_id, name FROM silver_entity_names"
            " WHERE snapshot_set_id = ? AND entity_id IN (SELECT unnest(?)) ORDER BY 1, 2",
            [ids],
        ):
            names.setdefault(entity_id, []).append(name)
        return names

    def add_rows(self, candidates: list[tuple[str, ...]], negative: bool = False) -> None:
        """(query, expected id, variant type, evidence, source name, entity type): kept only
        when the query is no name the graph holds, so the row tests matching, not lookup.
        A kept row with a source name is also a pair of one party."""
        held = self.holders(sorted({normalise_name(c[0]) for c in candidates}))
        taken: dict[str, int] = {}
        for query, expected, variant, evidence, original, entity_type in candidates:
            norm = normalise_name(query)
            # A legal form written out normalises as the name did: kept when only the
            # entity holds that form, so the row asks which of its names comes first.
            fresh = norm not in held or (variant == "legal_form" and held[norm] == {expected})
            if not norm or not fresh or taken.get(variant, 0) >= self.per_type:
                continue
            taken[variant] = taken.get(variant, 0) + 1
            self.labelled.append(
                {
                    "query_name": query,
                    "lei": "",
                    "jurisdiction": "",
                    "expected_entity_id": "" if negative else expected,
                    "variant_type": variant,
                    "set_id": self.store.set_id,
                    "evidence": evidence,
                    "confirm": "",
                }
            )
            if original:
                self.add_pair(query, original, entity_type, "1", variant, evidence)

    def add_pair(self, a: str, b: str, entity_type: str, same: str, variant: str, why: str) -> None:
        if (a, b) in self.paired:
            return
        self.paired.add((a, b))
        self.pairs.append(
            {
                "name_a": a,
                "name_b": b,
                "entity_type": entity_type,
                "same": same,
                "variant_type": variant,
                **{c: "" for c in PAIR_COLUMNS[5:9]},
                "set_id": self.store.set_id,
                "evidence": why,
                "confirm": "",
            }
        )

    def list_names_by_lei(self) -> None:
        """OpenSanctions records joined to GLEIF's entity by the LEI they state: the list's
        name and GLEIF's, a pair the LEI says is one. (Silver keeps the list's names as
        aliases, so they are no held-out queries.)"""
        rows = self.rows(
            "SELECT x.entity_id, n.legal_name, n.entity_type, b._raw->>'$.properties.name',"
            " b._raw->>'$.properties.alias', b._raw->>'$.properties.previousName'"
            " FROM silver_entity_xref x"
            " JOIN bronze_entities b ON b._snapshot_id = x.snapshot_id"
            "  AND b._source = x.source AND b._source_record_id = x.source_record_id"
            " JOIN graph_nodes n ON n.snapshot_set_id = x.snapshot_set_id AND n.id = x.entity_id"
            " WHERE x.snapshot_set_id = ? AND x.source = 'opensanctions' AND x.joined_by = 'LEI'"
            f" ORDER BY md5(x.source_record_id || '{self.seed}')"
        )
        pairs_by_type: dict[str, int] = {}
        for entity_id, legal_name, entity_type, *lists in rows:
            for text in lists:
                for name in csv_list(text):
                    if normalise_name(name) == normalise_name(legal_name):
                        continue
                    variant, _ = classify(name, [legal_name])
                    if pairs_by_type.get(variant, 0) < self.per_type:
                        pairs_by_type[variant] = pairs_by_type.get(variant, 0) + 1
                        why = f"OpenSanctions states this name with LEI {entity_id[4:]}"
                        self.add_pair(name, legal_name, entity_type, "1", variant, why)

    def romanised_cyrillic(self) -> None:
        """A Cyrillic legal name romanised by a table (BGN/PCGN-like), where the graph holds
        no such name: a query in Latin for a name written in Cyrillic."""
        rows = self.sample("regexp_matches(legal_name, '^[^A-Za-z]*[А-Яа-яЁёІіЇїЄєҐґ]')",
                           20 * self.per_type)  # fmt: skip
        self.add_rows(
            [
                (romanise(name), entity_id, "transliteration", f"'{name}' romanised", name, kind)
                for entity_id, name, kind in rows
            ]
        )

    def multi_script(self) -> None:
        """Entities whose source names them in a Latin and another script: the list's
        names in several scripts, and GLEIF's transliterated names."""
        rows = self.sample(
            "len(aliases) > 0 AND regexp_matches(legal_name || array_to_string(aliases, ' '),"
            " '[Ѐ-ӿ؀-ۿ一-鿿]')", 40 * self.per_type,
            "id, legal_name, entity_type, aliases",
        )  # fmt: skip
        taken: dict[str, int] = {}
        for entity_id, legal_name, entity_type, aliases in rows:
            names = [legal_name, *aliases]
            latin = [n for n in names if script_of(n) == "LATIN"]
            other = [n for n in names if script_of(n) not in ("LATIN", "NONE")]
            if not latin or not other:
                continue
            script = script_of(other[0]).lower()
            if script not in ("cyrillic", "arabic", "han") or taken.get(script, 0) >= self.per_type:
                continue
            taken[script] = taken.get(script, 0) + 1
            why = f"{entity_id} states both names ({script})"
            self.add_pair(latin[0], other[0], entity_type, "1", "transliteration", why)

    def written_variants(self) -> None:
        """Variants a rule writes from a real name; each names what the name names."""
        candidates = []
        company, person = "LegalEntity", "Person"
        for entity_id, name, _ in self.sample(
            "entity_type = 'LegalEntity'"
            " AND regexp_matches(legal_name, '^[A-Za-z][A-Za-z]{2,} [A-Za-z]{3,}')",
            10 * self.per_type,
        ):
            first, second, *rest = name.split(" ")
            query = " ".join([first + second, *rest])
            why = f"two words of '{name}' written as one"
            candidates.append((query, entity_id, "spacing", why, name, company))
        for i, (entity_id, name, _) in enumerate(self.sample(
            "entity_type = 'LegalEntity' AND regexp_matches(legal_name, '^[A-Za-z ]+$')"
            " AND len(string_split(legal_name, ' ')) BETWEEN 2 AND 5",
            10 * self.per_type,
        )):  # fmt: skip
            suffix = SUFFIXES[i % len(SUFFIXES)]
            why = f"'{suffix}' added to '{name}'"
            candidates.append((f"{name} {suffix}", entity_id, "generic_suffix", why, name, company))
        for entity_id, name, _ in self.sample(
            "entity_type = 'LegalEntity' AND regexp_matches(legal_name,"
            " ' (LLC|Ltd|LTD|JSC|PJSC|Inc|Corp|GmbH|AG|plc|PLC)\\.?$')",
            10 * self.per_type,
        ):
            *words, form = name.rstrip(".").split(" ")
            query = " ".join([*words, LEGAL_FORMS[form]])
            why = f"'{form}' of '{name}' written out"
            candidates.append((query, entity_id, "legal_form", why, name, company))
        for entity_id, name, _ in self.sample(
            "entity_type = 'Person' AND source = 'opensanctions'"
            " AND regexp_matches(legal_name, '^[A-Z][a-z]+( [A-Z][a-z]+){1,3}$')",
            10 * self.per_type,
        ):
            *given, last = name.split(" ")
            short = " ".join([*(g[0] + "." for g in given), last])
            why = f"given names of '{name}' as initials"
            candidates.append((short, entity_id, "initials", why, name, person))
        variants = {v: group for group in GIVEN_NAMES for v in group}
        for entity_id, name, _ in self._given(sorted(variants)):
            tokens = name.split(" ")
            hit = next(t for t in tokens if t in variants)
            other = next(v for v in variants[hit] if v != hit)
            query = " ".join(other if t == hit else t for t in tokens)
            why = f"'{hit}' of '{name}' spelt '{other}'"
            candidates.append((query, entity_id, "given_name", why, name, person))
        for entity_id, name, _ in self.sample(
            "regexp_matches(legal_name, '^[ -~]+$')", self.per_type
        ):
            # Verbatim: found by the exact-name path. Kept as a control.
            self.labelled.append({
                "query_name": name, "lei": "", "jurisdiction": "", "expected_entity_id": entity_id,
                "variant_type": "exact", "set_id": self.store.set_id,
                "evidence": "the entity's own name", "confirm": "",
            })  # fmt: skip
        self.add_rows(candidates)

    def _given(self, spellings: list[str]) -> list[tuple]:
        return self.rows(
            "SELECT id, legal_name, entity_type FROM graph_nodes WHERE snapshot_set_id = ?"
            " AND entity_type = 'Person' AND source = 'opensanctions'"
            " AND list_has_any(string_split(legal_name, ' '), ?)"
            f" ORDER BY md5(id || '{self.seed}') LIMIT {10 * self.per_type}",
            [spellings],
        )

    def near_homonyms(self) -> None:
        """An acronym with one letter changed: to a name nobody holds, or to another entity's."""
        rows = self.sample(
            "entity_type = 'LegalEntity' AND regexp_matches(legal_name, '^[A-Z]{3} [A-Z][a-z]+')",
            20 * self.per_type,
        )
        candidates, written = [], []
        for entity_id, name, _ in rows:
            acronym, rest = name.split(" ", 1)
            for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
                if letter != acronym[-1]:
                    written.append((f"{acronym[:-1]}{letter} {rest}", entity_id, name))
        held = self.holders(sorted({normalise_name(w[0]) for w in written}))
        pairs = 0
        for query, entity_id, name in written:
            others = held.get(normalise_name(query), set()) - {entity_id}
            if others and pairs < self.per_type:
                pairs += 1
                other = sorted(others)[0]
                self.add_pair(name, query, "LegalEntity", "0", "near_homonym",
                              f"{entity_id} and {other}: two LEIs, one letter apart")  # fmt: skip
        seen: set[str] = set()
        for query, entity_id, name in written:
            if entity_id not in seen:
                seen.add(entity_id)
                why = f"one letter from '{name}' ({entity_id}); no entity holds this name"
                candidates.append((query, "", "near_homonym", why, "", ""))
        self.add_rows(candidates, negative=True)

    def shared_registration(self) -> None:
        """GLEIF records of two LEIs on one authority and number: the person says `same`."""
        rows = self.rows(
            "WITH shared AS (SELECT registration_authority_id ra, registration_number num"
            " FROM graph_nodes WHERE snapshot_set_id = ? AND lei IS NOT NULL"
            " AND registration_number IS NOT NULL GROUP BY 1, 2 HAVING count(*) = 2)"
            " SELECT list(legal_name ORDER BY id), list(id ORDER BY id), any_value(entity_type)"
            " FROM graph_nodes n JOIN shared s ON n.registration_authority_id = s.ra"
            "  AND n.registration_number = s.num WHERE n.snapshot_set_id = ?"
            f" GROUP BY s.ra, s.num ORDER BY md5(s.num || '{self.seed}') LIMIT {self.per_type}",
            [self.store.set_id],
        )
        for (a, b), (id_a, id_b), entity_type in rows:
            self.add_pair(a, b, entity_type, "", "shared_registration",
                          f"{id_a} and {id_b} share an authority and number")  # fmt: skip


_CYRILLIC = dict(
    zip(
        "абвгдеёжзийклмнопрстуфхцчшщъыьэюяіїєґ",
        (
            "a b v g d e yo zh z i y k l m n o p r s t u f kh ts ch sh shch - y - e yu ya i yi ye g"
        ).split(" "),
        strict=True,
    )
)


def romanise(name: str) -> str:
    """Cyrillic letters in Latin (BGN/PCGN-like); a word in capitals stays in capitals."""
    out = []
    for word in re.split(r"(\s+)", name):
        capitals = word.isupper() and len(word) > 1
        for char in word:
            latin = _CYRILLIC.get(char.lower())
            if latin is None:
                out.append(char)
                continue
            latin = "" if latin == "-" else latin
            out.append(
                latin.upper() if capitals else latin.capitalize() if char.isupper() else latin
            )
    return "".join(out)


def csv_list(text: str | None) -> list[str]:
    return json.loads(text) if text else []


def write(path: Path, columns: tuple[str, ...], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, columns, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def draft(args: argparse.Namespace) -> None:
    with GraphStore.open(args.snapshot) as store:
        drafter = Drafter(store, args.per_type, args.seed)
        drafter.list_names_by_lei()
        drafter.romanised_cyrillic()
        drafter.multi_script()
        drafter.written_variants()
        drafter.near_homonyms()
        drafter.shared_registration()
    labelled_columns = tuple(c for c in LABELLED_COLUMNS if c != "labelled_by") + DRAFT_EXTRA
    pair_columns = tuple(c for c in PAIR_COLUMNS if c != "labelled_by") + DRAFT_EXTRA
    write(args.out / "labelled.csv", labelled_columns, drafter.labelled)
    write(args.out / "pairs.csv", pair_columns, drafter.pairs)
    for title, rows in (("labelled", drafter.labelled), ("pairs", drafter.pairs)):
        counts: dict[str, int] = {}
        for row in rows:
            counts[row["variant_type"]] = counts.get(row["variant_type"], 0) + 1
        shown = ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))
        print(
            f"{title}: {len(rows)} rows to confirm ({shown}) in {(args.out / title).as_posix()}.csv"
        )


def commit(args: argparse.Namespace) -> None:
    for draft_file, target, columns, key in (
        ("labelled.csv", LABELLED, LABELLED_COLUMNS, ("query_name", "expected_entity_id")),
        ("pairs.csv", PAIRS, PAIR_COLUMNS, ("name_a", "name_b")),
    ):
        source = args.out / draft_file
        if not source.is_file():
            continue
        with source.open(encoding="utf-8", newline="") as f:
            confirmed = [
                r for r in csv.DictReader(f) if r["confirm"].strip().lower() in ("y", "yes")
            ]
        unlabelled = [r for r in confirmed if "same" in r and r["same"] not in ("0", "1")]
        if unlabelled:
            sys.exit(f"{source.as_posix()}: {len(unlabelled)} confirmed pairs have no `same`.")
        existing: list[dict[str, str]] = []
        if target.is_file():
            with target.open(encoding="utf-8", newline="") as f:
                existing = list(csv.DictReader(f))
        seen = {tuple(r[k] for k in key) for r in existing}
        added = [
            {c: (args.by if c == "labelled_by" else r.get(c, "")) for c in columns}
            for r in confirmed
            if tuple(r[k] for k in key) not in seen
        ]
        write(target, columns, existing + added)
        print(f"{target.as_posix()}: {len(added)} rows added, {len(existing) + len(added)} in all")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    d = sub.add_parser("draft")
    d.add_argument("--snapshot", default="real")
    d.add_argument("--per-type", type=int, default=45)
    d.add_argument("--seed", default="step18")
    c = sub.add_parser("commit")
    c.add_argument("--by", required=True, help="Who confirmed the rows.")
    for p in (d, c):
        p.add_argument("--out", type=Path, default=Path("var/er_candidates"))
    args = parser.parse_args()
    draft(args) if args.command == "draft" else commit(args)


if __name__ == "__main__":
    main()
