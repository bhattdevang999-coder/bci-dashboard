"""NIS Rule Engine — universal parser + evaluator for Amazon NIS templates.

Modules:
  - nis_formula_parser:  Excel formula → AST
  - nis_rule_evaluator:  AST + state → verdict
  - nis_rule_extractor:  .xlsm → rule bundle (JSON)
  - nis_rule_engine:     Dashboard-facing API
"""

__all__ = [
    "nis_formula_parser",
    "nis_rule_evaluator",
    "nis_rule_extractor",
    "nis_rule_engine",
]
