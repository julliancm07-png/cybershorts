"""config.DEFAULTS must equal config.example.yaml, and an empty config.yaml must load every key."""
import os
import tempfile
import unittest
from pathlib import Path

import yaml

from cybershorts import config

REPO = Path(__file__).resolve().parent.parent
EXAMPLE = REPO / "config.example.yaml"
ENV_EXAMPLE = REPO / ".env.example"

# Every key added in v3, as a dotted path. Each must exist in DEFAULTS and in the example YAML.
V3_KEYS = [
    "voice.elevenlabs.tag_models",
    "voice.elevenlabs.audio_tags",
    "voice.elevenlabs.allowed_tags",
    "voice.elevenlabs.max_tags",
    "llm.hooks",
    "llm.hook_model",
    "youtube.first_comment",
    "series.enabled",
    "series.items",
    "data_card.enabled",
    "data_card.nvd",
    "data_card.seconds",
    "breaking.enabled",
    "breaking.kev",
    "breaking.rss_urgency",
    "breaking.urgency_threshold",
    "breaking.rss_max_age_minutes",
    "breaking.max_score_items",
    "breaking.urgency_model",
    "breaking.daily_cap",
    "breaking.quiet_hours",
    "breaking.timezone",
    "breaking.kev_max_age_days",
    "breaking.badge",
    "breaking.badge_color",
    "breaking.title_prefix",
    "analytics.enabled",
    "analytics.lookback_days",
    "analytics.min_age_hours",
    "analytics.min_videos",
    "analytics.top_n",
    "analytics.learnings_max_chars",
    "analytics.use_learnings",
    "analytics.report",
    "analytics.model",
]


def _diff(a, b, path="") -> list[str]:
    """Return human-readable differences between two parsed structures (strict on types)."""
    if isinstance(a, dict) and isinstance(b, dict):
        out = []
        for k in sorted(set(a) | set(b), key=str):
            p = f"{path}.{k}" if path else str(k)
            if k not in a:
                out.append(f"{p}: missing from DEFAULTS")
            elif k not in b:
                out.append(f"{p}: missing from example YAML")
            else:
                out.extend(_diff(a[k], b[k], p))
        return out
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return [f"{path}: list length {len(a)} != {len(b)}"]
        out = []
        for i, (x, y) in enumerate(zip(a, b)):
            out.extend(_diff(x, y, f"{path}[{i}]"))
        return out
    if type(a) is not type(b) or a != b:
        return [f"{path}: DEFAULTS={a!r} YAML={b!r}"]
    return []


def _get(raw: dict, dotted: str):
    cur = raw
    for part in dotted.split("."):
        cur = cur[part]
    return cur


class ConfigParityTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_app_dir = os.environ.get("APP_DIR")
        os.environ["APP_DIR"] = self._tmp.name

    def tearDown(self):
        if self._old_app_dir is None:
            os.environ.pop("APP_DIR", None)
        else:
            os.environ["APP_DIR"] = self._old_app_dir
        self._tmp.cleanup()

    def _example(self):
        return yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))

    def test_example_yaml_matches_defaults_recursively(self):
        diffs = _diff(config.DEFAULTS, self._example())
        self.assertEqual(diffs, [], "DEFAULTS and config.example.yaml differ:\n" + "\n".join(diffs))

    def test_example_yaml_loads_as_mapping(self):
        self.assertIsInstance(self._example(), dict)

    def test_v3_keys_exist_in_defaults_and_example(self):
        example = self._example()
        for dotted in V3_KEYS:
            with self.subTest(key=dotted):
                _get(config.DEFAULTS, dotted)
                _get(example, dotted)

    def test_empty_config_yaml_loads_every_key(self):
        (Path(self._tmp.name) / "config.yaml").write_text("", encoding="utf-8")
        cfg = config.load()
        diffs = _diff(config.DEFAULTS, cfg.raw)
        self.assertEqual(diffs, [], "empty config.yaml did not yield DEFAULTS:\n" + "\n".join(diffs))
        for dotted in V3_KEYS:
            with self.subTest(key=dotted):
                _get(cfg.raw, dotted)

    def test_config_yaml_overrides_one_v3_key_and_keeps_the_rest(self):
        (Path(self._tmp.name) / "config.yaml").write_text(
            "breaking:\n  daily_cap: 5\n", encoding="utf-8")
        cfg = config.load()
        self.assertEqual(cfg.get("breaking.daily_cap"), 5)
        self.assertEqual(cfg.get("breaking.badge"), "BREAKING")
        self.assertEqual(cfg.get("series.items")[0]["hashtag"], "PatchThisNow")

    def test_env_example_lists_exactly_the_secret_keys(self):
        keys = set()
        for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            keys.add(line.split("=", 1)[0].strip())
        self.assertEqual(keys, set(config.SECRET_KEYS))

    def test_nvd_api_key_is_a_secret(self):
        self.assertIn("NVD_API_KEY", config.SECRET_KEYS)
        self.assertIn("NVD_API_KEY=", ENV_EXAMPLE.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
