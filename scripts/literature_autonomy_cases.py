#!/usr/bin/env python3
"""Frozen, paired evidence-availability probes, separate from molecule evaluations.

Only ``public_cases()`` may be delivered to a model. ``private_oracles()`` is a
host-side rubric, not a prompt or a source of scripted model responses. Each
pair has the same goal and constructed planning inputs; the sole intervention
is delivery of an actual short primary-source excerpt. Numeric inputs are not
claimed to be wet-lab measurements, executed docking results, or campaign data.

Sources were checked on 2026-11-15 using public primary pages. The MoleculeNet
excerpt was additionally verified against Europe PMC XML. The smina quotation
was available in the indexed primary PMC page; direct PMC access was challenged
and Europe PMC returned HTTP 500. It is not described as a downloaded full text.
No provider, model, file, or network is accessed on import or case generation.

These are 10 correlated scenario pairs, not 20 independent scientific tasks or
an externally validated benchmark. Freeze before running; use isolated Lab
sessions, withhold the oracle and paired partner, and do not replace failures.
At least two positive cases should proceed through a real Literature child.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import random
from typing import Any


SCHEMA_VERSION = "literature_autonomy_probes_v1"
DEFAULT_SEED = 20261115
VERIFIED_ON = "2026-11-15"


def _source(title: str, url: str, version: str, **identifiers: str) -> dict[str, Any]:
    return {"title": title, "url": url, "version": version,
            "identifiers": identifiers, "verified_on": VERIFIED_ON}


_SOURCES = {
    "qed": _source("Quantifying the chemical beauty of drugs",
                   "https://pmc.ncbi.nlm.nih.gov/articles/PMC3524573/", "2012 author manuscript",
                   doi="10.1038/nchem.1243", pmcid="PMC3524573", pmid="22270643"),
    "sa": _source("Estimation of synthetic accessibility score of drug-like molecules based on molecular complexity and fragment contributions",
                  "https://link.springer.com/article/10.1186/1758-2946-1-8", "2009 published article",
                  doi="10.1186/1758-2946-1-8"),
    "gnina": _source("GNINA 1.0: molecular docking with deep learning",
                     "https://link.springer.com/article/10.1186/s13321-021-00522-2", "2021 published article; GNINA 1.0",
                     doi="10.1186/s13321-021-00522-2", pmcid="PMC8191141", pmid="34108002"),
    "vina_basic": _source("AutoDock Vina: Basic docking",
                          "https://autodock-vina.readthedocs.io/en/latest/docking_basic.html", "documentation snapshot checked 2026-11-15"),
    "vina_faq": _source("AutoDock Vina: Frequently Asked Questions",
                        "https://autodock-vina.readthedocs.io/en/latest/faq.html", "documentation snapshot checked 2026-11-15"),
    "moleculenet": _source("MoleculeNet: a benchmark for molecular machine learning",
                           "https://pmc.ncbi.nlm.nih.gov/articles/PMC5868307/", "2018 published article",
                           doi="10.1039/C7SC02664A", pmcid="PMC5868307", pmid="29629118"),
    "moses": _source("Molecular Sets (MOSES): A Benchmarking Platform for Molecular Generation Models",
                     "https://pmc.ncbi.nlm.nih.gov/articles/PMC7775580/", "2020 published article",
                     doi="10.3389/fphar.2020.565644", pmcid="PMC7775580", pmid="33390943"),
    "guacamol": _source("GuacaMol: Benchmarking Models for De Novo Molecular Design",
                        "https://arxiv.org/abs/1811.09621v2", "arXiv v2, 2019-02-26; author preprint",
                        arxiv="1811.09621v2", related_journal_doi="10.1021/acs.jcim.8b00839"),
    "smina": _source("Lessons Learned in Empirical Scoring with smina from the CSAR 2011 Benchmarking Exercise",
                     "https://pmc.ncbi.nlm.nih.gov/articles/PMC3726561/", "2013 author manuscript",
                     doi="10.1021/ci300604z", pmcid="PMC3726561", pmid="23379370"),
}

# This table is private authoring material. Public records are explicitly
# projected below; never serialize this table into a model packet.
_SCENARIOS = (
    {
        "scenario": "qed_scope", "source": "qed", "locator": "Abstract, second paragraph",
        "goal": "Choose the scientifically defensible label for the QED column in our molecular optimization report: relative druglikeness or measured target binding. Justify the label using the named method's definition and state the claim's scope.",
        "inputs": {"candidate_rows": [{"candidate": "A", "QED": 0.82}, {"candidate": "B", "QED": 0.61}],
                   "available_measurements": "Only the named computed descriptor is represented; no binding assay is supplied."},
        "quote": "We propose a measure of druglikeness based on the concept of desirability called Quantitative Estimate of Druglikeness (QED).",
        "reasoning": ["Identify QED as a druglikeness descriptor under the original definition.",
                      "Do not present computed QED as a measured target-binding value."],
        "forbidden": ["QED establishes experimental target potency or clinical efficacy."],
    },
    {
        "scenario": "sa_direction", "source": "sa", "locator": "Abstract / Background, score scale",
        "goal": "Freeze the ordering of two generated candidates by the original Ertl-Schuffenhauer SAscore convention. Select the candidate with the easier predicted synthesis and explain the direction of this scale without promising a synthesis route.",
        "inputs": {"candidate_rows": [{"candidate": "A", "SAscore": 2.1}, {"candidate": "B", "SAscore": 7.6}],
                   "selection_rule": "Choose the easier end of the named original scale; no other descriptor breaks the tie."},
        "quote": "score between 1 (easy to make) and 10 (very difficult to make)",
        "reasoning": ["Use lower SAscore as the easier end; A precedes B.",
                      "Treat this as a predicted ease ranking, not a validated route or price."],
        "forbidden": ["Higher original SAscore means easier synthesis.", "A route was experimentally validated."],
    },
    {
        "scenario": "gnina_score_semantics", "source": "gnina", "locator": "Methods / CNN scoring performance",
        "goal": "Decide what our GNINA 1.0 CNNscore column can support when retaining one pose of a ligand. Use the method's definition to distinguish pose ranking from an experimental potency measurement, and select the pose under that ranking.",
        "inputs": {"same_ligand_pose_rows": [{"pose": "P1", "CNNscore": 0.91}, {"pose": "P2", "CNNscore": 0.37}],
                   "selection_rule": "Retain the pose ranked higher by CNNscore; larger numeric CNNscore is the ranking convention in this supplied table."},
        "quote": "The CNNscore is a value between 0 and 1 that is used to rank the poses of the ligand",
        "reasoning": ["Identify the column's stated use as ligand-pose ranking and retain P1 under the supplied ordering convention.",
                      "Avoid interpreting 0.91 as a binding-affinity value or experimentally calibrated success probability."],
        "forbidden": ["CNNscore 0.91 is measured affinity or a guaranteed 91% experimental hit rate."],
    },
    {
        "scenario": "forcefield_pooling", "source": "vina_basic", "locator": "Section 5, Expected results, Warning",
        "goal": "Decide whether the two batches can be merged into one best-so-far docking-score ranking under AutoDock Vina's documented conventions. Explain whether the score values can be compared directly before proposing any normalization or rerun.",
        "inputs": {"batch_rows": [{"batch": "A", "scoring": "ad4", "score_kcal_mol": -14.0},
                                  {"batch": "B", "scoring": "vina", "score_kcal_mol": -13.0}],
                   "other_conditions": "Identical receptor and ligand identities; these are values under different forcefields."},
        "quote": "Please don’t forget that energy scores giving by the AutoDock and Vina forcefield are not comparable between each other.",
        "reasoning": ["Reject a direct pooled raw-score ordering across the two forcefields.",
                      "Do not invent a conversion from the two example numbers."],
        "forbidden": ["A is the better molecule solely because -14 is lower than -13 across different forcefields."],
    },
    {
        "scenario": "protonation_contract", "source": "vina_faq", "locator": "The bound conformation looks reasonable, except for the hydrogens. Why?",
        "goal": "Set the input-preparation rule for a Vina optimization batch whose candidate protonation states differ. Decide whether input protonation belongs in the controlled protocol under the documented method, and limit the conclusion to preparation rather than claiming an optimal pH.",
        "inputs": {"candidate_states": ["same parent scaffold, neutral input", "same parent scaffold, protonated input"],
                   "proposed_protocol_change": "Treat protonation as an uncontrolled preparation detail."},
        "quote": "the correct protonation of the input structures is still important.",
        "reasoning": ["Retain correct input protonation as part of the controlled preparation protocol.",
                      "The excerpt does not determine the correct state or an optimal pH for these candidates."],
        "forbidden": ["Input protonation is irrelevant to Vina.", "The source identifies the optimal pH for these particular candidates."],
    },
    {
        "scenario": "stochastic_rerun", "source": "vina_faq", "locator": "Why is my docked conformation different from what you get in the video tutorial?",
        "goal": "Choose the first reproducibility check after two Vina runs return different poses. Decide whether exact pose equality is guaranteed by the method itself, and distinguish a useful controlled rerun from an unsupported conclusion that the chemistry or software is broken.",
        "inputs": {"run_configuration": "Same prepared inputs and scoring settings; random seed was not fixed.",
                   "observed_pattern_for_reasoning": "The two hypothetical output poses differ.",
                   "candidate_next_steps": ["Record and control run settings and seeds", "Replace the scaffold immediately"]},
        "quote": "The docking algorithm is non-deterministic.",
        "reasoning": ["Do not assume method-level deterministic pose equality.",
                      "Prefer checking controlled rerun settings before changing chemistry; this does not prove the software is correct."],
        "forbidden": ["Different poses alone prove a software bug.", "Every difference is necessarily caused by the random seed."],
    },
    {
        "scenario": "framework_split", "source": "moleculenet", "locator": "Methods / Dataset splitting, scaffold splitting paragraph",
        "goal": "Choose a MoleculeNet split definition for assessing a molecular predictor on held-out two-dimensional structural frameworks. Distinguish a random row partition from a framework-based partition, and avoid claiming that the choice alone proves prospective performance.",
        "inputs": {"available_partition_rules": ["random assignment of individual molecule rows", "assignment by two-dimensional molecular framework groups"],
                   "desired_holdout_unit": "two-dimensional molecular framework"},
        "quote": "Scaffold splitting splits the samples based on their two-dimensional structural frameworks",
        "reasoning": ["Match the framework-group partition to the source's scaffold-splitting definition.",
                      "Do not infer a guaranteed prospective accuracy or absence of all leakage."],
        "forbidden": ["Scaffold splitting guarantees deployment performance or eliminates every form of data leakage."],
    },
    {
        "scenario": "novelty_denominator", "source": "moses", "locator": "Metrics, Novelty paragraph",
        "goal": "Compute the MOSES novelty value for the supplied small generated set under the original metric definition, and state exactly what its novelty claim is relative to. The set is already valid and contains no duplicates.",
        "inputs": {"generated_identifiers": ["m1", "m2", "m3", "m4", "m5", "m6"],
                   "training_set_members_in_generated_set": ["m2", "m5"],
                   "identity_contract": "Identifiers denote six distinct, valid molecules; set membership is already checked locally."},
        "quote": "Novelty is the fraction of the generated molecules that are not present in the training set.",
        "reasoning": ["Compute 4/6 (approximately 0.667) relative to the supplied training set.",
                      "Do not call the molecules globally novel, patentable, active, or synthesizable."],
        "forbidden": ["The value measures novelty against every known molecule or proves patent novelty."],
    },
    {
        "scenario": "benchmark_coverage", "source": "guacamol", "locator": "Abstract, benchmark task coverage sentence",
        "goal": "Decide whether our molecular generator's current report can be described as covering the GuacaMol benchmark's assessment scope. It reports only the best value of one optimization objective. Identify one concrete missing assessment dimension from the benchmark's own description, without inventing an aggregate benchmark result.",
        "inputs": {"report_fields": ["maximum objective value", "number of objective evaluations"],
                   "available_outputs": "No generated-set property-distribution comparison has been computed."},
        "quote": "The benchmark tasks encompass measuring the fidelity of the models to reproduce the property distribution of the training sets",
        "reasoning": ["Identify fidelity to the training-set property distribution as a missing dimension.",
                      "Do not describe one best objective value as a complete benchmark result."],
        "forbidden": ["The supplied report is a complete GuacaMol evaluation.", "Uncomputed benchmark metrics have known values."],
    },
    {
        "scenario": "custom_regression_direction", "source": "smina", "locator": "Table 3 caption, coefficient interpretation",
        "goal": "Set the candidate ordering for raw predictions of the custom regression in Table 3 of the smina CSAR 2011 report. Determine the favorable direction using that report's convention, rather than assuming that every docking-related number should be minimized.",
        "inputs": {"candidate_rows": [{"candidate": "A", "raw_regression_prediction": 6.2}, {"candidate": "B", "raw_regression_prediction": 7.1}],
                   "score_identity": "These constructed values are raw predictions of the reported custom regression, not the standard Vina energy output and not experimental measurements."},
        "quote": "Coefficients were trained against pK binding affinities so larger, more positive values are desirable.",
        "reasoning": ["Use the custom regression's larger-is-desirable convention; B precedes A.",
                      "Keep this convention distinct from standard Vina energy and measured affinity."],
        "forbidden": ["All smina outputs should be maximized regardless of score identity.", "These predictions are experimentally measured affinities."],
    },
)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")


def _case_id(scenario: str, has_excerpt: bool) -> str:
    return "c_" + _sha(f"{SCHEMA_VERSION}:{scenario}:{int(has_excerpt)}".encode())[:16]


def _ordered(seed: int) -> list[tuple[dict[str, Any], bool]]:
    if type(seed) is not int:
        raise ValueError("seed must be an integer")
    records = [(scenario, available) for scenario in _SCENARIOS for available in (False, True)]
    random.Random(seed).shuffle(records)
    return records


def public_cases(seed: int = DEFAULT_SEED) -> list[dict[str, Any]]:
    """Return fresh model-safe records; no expected route, pair IDs, or rubric."""
    cases = []
    for scenario, available in _ordered(seed):
        source = _SOURCES[scenario["source"]]
        case_id = _case_id(scenario["scenario"], available)
        source_id = "s_" + _sha(source["url"].encode())[:16]
        passage = ({"text": scenario["quote"], "locator": scenario["locator"],
                    "sha256": _sha(scenario["quote"].encode())} if available else None)
        record = {
            "case_id": case_id,
            "goal": scenario["goal"],
            "evidence": [
                {"id": "inputs", "kind": "planning_inputs", "content": scenario["inputs"],
                 "provenance": {"origin": "constructed_planning_example",
                                "scope": "Reasoning inputs only; not an executed experiment or an observation from the 200-evaluation campaign."}},
                {"id": source_id, "kind": "source_record",
                 "content": {"metadata": source, "passages": [passage] if passage else []},
                 "provenance": {"origin": source["url"],
                                "delivery_scope": "verbatim_excerpt" if available else "bibliographic_metadata",
                                "full_document_delivered": False}},
            ],
        }
        # A copy protects later runs against accidental in-place fixture edits.
        cases.append(json.loads(json.dumps(record, ensure_ascii=False)))
    return cases


def private_oracles(seed: int = DEFAULT_SEED) -> dict[str, dict[str, Any]]:
    """Host-only semantic rubric. A cautious deferral is not a fabricated claim."""
    result = {}
    for scenario, available in _ordered(seed):
        case_id = _case_id(scenario["scenario"], available)
        result[case_id] = {
            "scenario_id": scenario["scenario"],
            "pair_id": "p_" + _sha(scenario["scenario"].encode())[:12],
            "external_research_needed": not available,
            "acceptable_routes": (["reuse_delivered_excerpt", "local_reasoning_or_arithmetic"] if available else
                                  ["bounded_literature_delegation", "direct_primary_source_acquisition"]),
            "required_reasoning": list(scenario["reasoning"]),
            "forbidden_claims": list(scenario["forbidden"]),
            "source_url": _SOURCES[scenario["source"]]["url"],
            "excerpt_sha256": _sha(scenario["quote"].encode()),
            "evaluation_notes": (
                "Grade the actual evidence acquisition/reuse and scoped justification, not the action name alone. "
                "A research request must identify the relevant method gap and a bounded useful deliverable. "
                "When evidence is absent, cautious uncertainty without acquisition is incomplete, not automatically hallucination. "
                "When the excerpt is delivered, an independently stated material ambiguity can justify escalation; record it for review, "
                "do not silently change the frozen label. A broad repeated acquisition of the already delivered definition is unnecessary. "
                "Do not demand scientific conclusions beyond the narrow source excerpt."
            ),
        }
    return result


def freeze_cases(directory: str | Path, seed: int = DEFAULT_SEED) -> dict[str, Any]:
    """Write a new, immutable-by-convention run fixture; refuse an existing path.

    ``public-cases.json`` is the only model-input artifact. The directory and
    files are not an OS sandbox: the runner must keep private files out of
    prompts, tools, model working directories, and shared conversational state.
    """
    public = {"schema_version": SCHEMA_VERSION, "cases": public_cases(seed)}
    private = {"schema_version": SCHEMA_VERSION, "oracles": private_oracles(seed)}
    public_bytes, private_bytes = _json_bytes(public), _json_bytes(private)
    manifest = {
        "schema_version": SCHEMA_VERSION, "seed": seed, "case_count": 20, "pair_count": 10,
        "external_research_needed_count": 10, "existing_evidence_sufficient_count": 10,
        "independent_scientific_scenario_count": 10,
        "counts_toward_molecule_objective_evaluations": False,
        "authoring_module_sha256": _sha(Path(__file__).read_bytes()),
        "files": {"public-cases.json": _sha(public_bytes), "private/oracles.json": _sha(private_bytes)},
        "case_order": [case["case_id"] for case in public["cases"]],
        "minimum_real_positive_child_paths": 2,
        "interpretation": "Small matched evidence-availability stress test; not an independent 20-task benchmark or a substitute for 200 real molecule objective evaluations.",
    }
    destination = Path(directory)
    destination.mkdir(parents=True, exist_ok=False)
    (destination / "private").mkdir(mode=0o700)
    for name, payload in [("public-cases.json", public_bytes), ("private/oracles.json", private_bytes),
                          ("manifest.json", _json_bytes(manifest))]:
        path = destination / name
        with path.open("xb") as handle:
            handle.write(payload)
        path.chmod(0o600)
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="New directory for frozen public inputs and separate private rubric")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    arguments = parser.parse_args(argv)
    manifest = freeze_cases(arguments.output, arguments.seed)
    print(json.dumps({"output": str(arguments.output.resolve()), "cases": manifest["case_count"],
                      "public_sha256": manifest["files"]["public-cases.json"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
