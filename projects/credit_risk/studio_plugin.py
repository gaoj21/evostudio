"""Credit-risk monitoring as a Studio project plugin.

Everything Studio shows for this project comes from here: the dataset Input,
the "CR" preset nodes, the monitoring template and the obligor lookup
toolkit. Studio itself knows none of these names (backend/features/plugins.py).
"""

NAME = "Credit risk"
DESCRIPTION = "Credit-risk monitoring: dataset Input, preset nodes, template and obligor lookup."


def presets():
    from credit_risk.studio.presets import credit_risk_presets
    return credit_risk_presets()


def templates():
    from credit_risk.studio.presets import templates as risk_templates
    return risk_templates()


def toolkits():
    def factory(**kwargs):
        from credit_risk.studio.obligor_tool import ObligorMatchToolkit
        return ObligorMatchToolkit(**kwargs)
    return {
        "ObligorMatchToolkit": {
            "factory": factory,
            "description": "Match company names / SEC CIKs against the internal credit-risk obligor list (match_company_name, match_cik).",
            "requires": [],
            "tool_names": ["match_company_name", "match_cik"],
        },
    }


def _records(config):
    from credit_risk.studio.feed import credit_risk_records
    n_raw = config.get("n")
    return credit_risk_records(
        split=config.get("split") or None,
        n=int(n_raw) if n_raw not in (None, "") else 1,  # n=0: entire split
        seed=int(config.get("seed") or 42),
        step=config.get("step"),
        **({"dataset": config["dataset"]} if config.get("dataset") else {}),
    )


def _info(params):
    from credit_risk.studio.feed import credit_risk_info
    return credit_risk_info(params.get("dataset") or None)


def source_types():
    from credit_risk.studio.feed import CREDIT_RISK_SAMPLES
    from credit_risk.studio.releases import catalog
    available = (["contemporary"] if CREDIT_RISK_SAMPLES.is_file() else []) + [item["id"] for item in catalog()]
    return {
        "credit_risk": {
            "label": "Credit Risk Feed",
            "description": "Choose a dataset version and split. Releases emit dated observations in trajectory order.",
            "config": [
                {"name": "dataset", "label": "Dataset version", "type": "select",
                 "default": available[0] if available else "", "options": available},
                {"name": "split", "label": "Split", "type": "select",
                 "options": ["", "dev", "test"], "default": ""},
                {"name": "n", "label": "Trajectories (0 = entire split)", "type": "number", "default": 1},
                {"name": "seed", "label": "Seed", "type": "number", "default": 42},
                {"name": "step", "label": "Observation period", "type": "select",
                 "options": ["none", "monthly", "weekly", "daily"], "default": "monthly"},
            ],
            "outputs": ["sample_id", "company", "symbol", "cik", "window_start",
                        "window_end", "as_of", "news_batch", "filing_batch",
                        "sample_json"],
            "records": _records,
            "info": _info,
            # One trajectory is one company; its observations run in date
            # order, and a failed observation blocks the rest of it.
            "sequence": {"group": "sample_id", "order": "as_of"},
            "watch_key": "sample_id",
        },
    }
