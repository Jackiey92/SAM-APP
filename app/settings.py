"""Central feature gates. Facts-tree writes are opt-in, never auto-enabled."""
import os

SAM_FACTS_TREE = os.environ.get("SAM_FACTS_TREE", "").lower() in {"1", "true", "yes", "on"}
