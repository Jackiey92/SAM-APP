"""/api/knowledge is a public boundary: mapping internals never leave the host."""
from __future__ import annotations

from app.knowledge import KnowledgeService
from tests.test_knowledge import COMPANY, _assert_no_key, _without_key, knowledge_env, seed_file


def test_api_knowledge_redacts_mapping_and_keeps_public_fields(knowledge_env):
    client, db, objects, memory = knowledge_env
    stored, _ = seed_file(knowledge_env)

    # The seeded company really has rendered L2 mappings before redaction;
    # otherwise the boundary assertions below would be vacuous.
    raw = KnowledgeService(db, memory, objects_path=objects).read(company_id=COMPANY)
    raw_file = raw["evidence"]["files"][0]
    assert raw_file["l2"]["mapping"]
    assert raw_file["l2"]["pages"][0]["mapping"]

    response = client.get("/api/knowledge")
    assert response.status_code == 200
    data = response.json
    _assert_no_key(data, "l2_manifest_uri")
    file_entry = data["evidence"]["files"][0]
    assert "mapping" not in file_entry["l2"]
    assert "manifest_uri" not in file_entry["l2"]
    assert file_entry["l2"]["pages"]
    for page in file_entry["l2"]["pages"]:
        assert "mapping" not in page

    # Non-mapping fields survive verbatim: file identity, source_ref URLs,
    # page numbers and fact coordinates all match the unredacted read().
    assert file_entry["file_hash"] == stored.file_hash
    assert file_entry["source_ref"] == {k: v for k, v in raw_file["source_ref"].items()
                                        if k != "l2_manifest_uri"}
    for projected, original in zip(file_entry["l2"]["pages"], raw_file["l2"]["pages"]):
        assert projected["page"] == original["page"]
        assert projected["source_ref"] == {k: v for k, v in original["source_ref"].items()
                                           if k != "l2_manifest_uri"}
    assert data["facts"]["groups"] == _without_key(raw["facts"]["groups"], "l2_manifest_uri")
