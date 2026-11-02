"""§8-11/12: fixed local H only; backup H/R installation and W recovery deferred."""
import pytest

from tests import test_projection_fault_drills_d15 as loss
from tests.test_projection_resources_d15 import _run_full
from tests.test_projection_sigkill_d15 import _run_drill


def test_all_projection_files_lost_rebuild_standard_wide_fts(scene):
    loss.test_e1_loss_and_explicit_dangling_pointer_recovery(scene)


def test_rebuilt_consumption_cannot_be_accepted_twice(scene):
    loss.test_e2_rebuilt_projection_does_not_release_consumed_candidate(scene)


@pytest.mark.parametrize('stage', ['standard-dml', 'derived-dml', 'before-pointer', 'after-pointer'])
def test_projection_real_sigkill_and_fresh_restart(drill, stage, tmp_path):
    _run_drill(drill, stage, tmp_path)


@pytest.mark.parametrize('stage', ['standard', 'derived'])
def test_namespace_tmpfs_sqlite_full_keeps_old_pair_then_retries(resource_scene, stage):
    # D15 observes real SQLITE_FULL=13 and independently probes ENOSPC=28,
    # then frees the filler before retrying and comparing canonical/wide/FTS.
    _run_full(resource_scene, stage)
