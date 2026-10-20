"""Flask-only cloud window: snapshot reads, global auth and mutation gate.

Endpoint selection here is UI wiring, not business classification. The cloud
never interprets fact values or derives dashboard cards; it displays fields
assembled by the existing local services, including future/unknown fields.
"""
from __future__ import annotations

import hmac
import json
import os
import sqlite3
from flask import abort, g, render_template, request

from .cloud_snapshot import (MAX_BODY_BYTES, SnapshotKnowledgeService, SnapshotStore,
                             object_value, reject_constant, validate_snapshot)
from .knowledge import mapping_redacted_projection

from .module_taxonomy import module_definitions

READONLY_MESSAGE = "云端为只读演示窗口，请在本地操作"


def _project_knowledge(envelope: dict) -> None:
    """Redact mapping internals from a snapshot envelope's knowledge section.

    The projection deep-copies, so the stored document is never mutated here;
    only the in-request copy is redacted. Every other knowledge field survives.
    """
    payload = envelope.get('payload') if isinstance(envelope, dict) else None
    if isinstance(payload, dict) and isinstance(payload.get('knowledge'), dict):
        payload['knowledge'] = mapping_redacted_projection(payload['knowledge'])


def current_snapshot(app):
    if "sam_cloud_snapshot" not in g:
        snapshot = app.extensions["sam_snapshot_store"].read()
        company_id = os.environ.get('SAM_COMPANY_ID', '').strip()
        if snapshot and company_id and snapshot.get('company_id') != company_id:
            snapshot = None  # a persisted snapshot from a previous host scope
        if snapshot:
            # Snapshots stored before ingest-side redaction still carry mapping
            # internals; project them for this request only, never write back.
            _project_knowledge(snapshot)
        g.sam_cloud_snapshot = snapshot
    return g.sam_cloud_snapshot


def _visible_modules(context):
    static = module_definitions(include_hidden=False)
    if 'visible_modules' not in context:
        return static
    supplied = context['visible_modules']
    if not isinstance(supplied, list):
        return []
    allowed = {m['code'] for m in static}
    selected = {item['code'] for item in supplied
                if isinstance(item, dict) and isinstance(item.get('code'), str)
                and item['code'] in allowed}
    # Snapshot controls membership only; all display fields and order are local.
    return [m for m in static if m['code'] in selected]


def page_context(app) -> dict:
    snapshot = current_snapshot(app) or {}
    payload = object_value(snapshot.get("payload"))
    context = object_value(payload.get("page_context"))
    # Do not unpack untrusted page_context into template control variables.
    # Its unknown fields are still preserved and shown in the raw snapshot.
    return {"company_id": snapshot.get("company_id") or os.environ.get("SAM_COMPANY_ID", ""),
            "facts": context.get("facts") if isinstance(context.get("facts"), list) else [],
            "todos": context.get("todos") if isinstance(context.get("todos"), list) else [],
            "files": context.get("files") if isinstance(context.get("files"), list) else [],
            "module_counts": object_value(context.get("module_counts")),
            "module_labels": object_value(context.get("module_labels")),
            "visible_modules": _visible_modules(context)}


def _dedicated_facts_tree_section(app, company_id):
    """The dedicated FactsTree section, only with its contract marker intact.

    DEC-020 option A: a ready section without a valid placement_validation
    marker (missing, old structure, foreign company/binding fields or a
    digest mismatch) is ledger damage from the cloud's perspective and fails
    closed with the API-001 consistency error; old snapshots are never served
    as a fallback. Explicitly unbound sections carry no tree content and need
    no marker.
    """
    store = app.extensions.get('sam_facts_tree_snapshot_store')
    envelope = store.read() if store is not None else None
    if not envelope or envelope.get('company_id') != company_id:
        # No dedicated snapshot, or one persisted under a previous host scope.
        return None, ({"error": "facts_tree_unavailable",
                       "message": "快照中没有本公司的事实树数据"}, 503)
    section = object_value(object_value(envelope.get('payload')).get('facts_tree'))
    if not section or section.get('company_id') != company_id:
        # Missing section or a cross-company section fails closed.
        return None, ({"error": "facts_tree_unavailable",
                       "message": "快照中没有本公司的事实树数据"}, 503)
    if section.get('state') == 'unbound':
        return section, None
    from .facts_tree.tree_read import verify_placement_validation
    try:
        verify_placement_validation(section, company_id)
    except ValueError as exc:
        return None, ({"error": "consistency_failed", "message": str(exc)}, 409)
    return section, None


def facts_tree_response(app):
    """Cloud API-001: read only the dedicated FactsTree snapshot store.

    The homepage snapshot is never a fallback source, even when it carries a
    payload.facts_tree section; the local DB is never touched either.
    """
    company_id = os.environ.get('SAM_COMPANY_ID', '').strip()
    body = request.get_json(silent=True)
    if ("company_id" in request.args or "company_id" in request.form
            or (isinstance(body, dict) and "company_id" in body)):
        return {"error": "company_id_rejected",
                "message": "公司范围只能由宿主注入，不接受 company_id 参数"}, 400
    node = request.args.get("node", "tree")
    code = request.args.get("code")
    if (set(request.args) - {"node", "code"} or node not in {"tree", "trunk", "leaf"}
            or (node != "tree" and not code) or (node == "tree" and code is not None)):
        return {"error": "invalid_parameter",
                "message": "node 仅支持 tree/trunk/leaf；trunk/leaf 必须提供 code"}, 400
    if not company_id:
        return {"error": "company_scope_missing", "message": "宿主未注入公司范围"}, 503
    from . import settings
    if not settings.SAM_FACTS_TREE:
        # Old dedicated data stays invisible while the feature gate is off.
        return {"error": "facts_tree_disabled", "message": "facts tree is disabled"}, 503
    section, error = _dedicated_facts_tree_section(app, company_id)
    if error is not None:
        return error
    if section.get('state') == 'unbound':
        return {'company_id': company_id, 'binding': None, 'node': node,
                'code': code if node != 'tree' else None, 'state': 'unbound'}
    if node == 'tree':
        result = section.get('tree')
    else:
        result = object_value(section.get('trunks' if node == 'trunk' else 'leaves')).get(code)
    if not isinstance(result, dict):
        return {"error": f"unknown_{node}", "message": "当前绑定模板中没有该节点"}, 404
    return result


def facts_tree_source_response(app):
    """Cloud API-002: resolve one leaf citation from the dedicated snapshot.

    Locator metadata comes only from the snapshot's verified leaf document.
    Snapshots carry no original binaries and no source name table, so the
    original is always reported unavailable; the homepage snapshot is never
    a fallback and the local DB is never touched.
    """
    company_id = os.environ.get('SAM_COMPANY_ID', '').strip()
    body = request.get_json(silent=True)
    if ("company_id" in request.args or "company_id" in request.form
            or (isinstance(body, dict) and "company_id" in body)):
        return {"error": "company_id_rejected",
                "message": "公司范围只能由宿主注入，不接受 company_id 参数"}, 400
    leaf_code = request.args.get('leaf_code')
    ref = request.args.get('ref')
    if set(request.args) - {'leaf_code', 'ref'} or not leaf_code or not ref:
        return {"error": "invalid_parameter",
                "message": "必须提供 leaf_code 与 ref，且不接受其他参数"}, 400
    if not company_id:
        return {"error": "company_scope_missing", "message": "宿主未注入公司范围"}, 503
    from . import settings
    if not settings.SAM_FACTS_TREE:
        # Old dedicated data stays invisible while the feature gate is off.
        return {"error": "facts_tree_disabled", "message": "facts tree is disabled"}, 503
    section, error = _dedicated_facts_tree_section(app, company_id)
    if error is not None:
        return error
    if section.get('state') == 'unbound':
        return {'company_id': company_id, 'binding': None, 'node': 'leaf',
                'code': leaf_code, 'state': 'unbound'}
    result = object_value(section.get('leaves')).get(leaf_code)
    if not isinstance(result, dict):
        return {"error": "unknown_leaf", "message": "当前绑定模板中没有该叶子"}, 404
    if result.get('state') != 'ready':
        return result  # API-001 style explicit state (e.g. not_generated)
    content = result.get('content')
    if not isinstance(content, str):
        return {"error": "consistency_failed", "message": "快照叶正文缺失或损坏"}, 409
    index = object_value(result.get('index'))
    from .facts_tree.tree_read import TreeReadError, resolve_source_ref
    try:
        source = resolve_source_ref(content, set(index.get('sources') or []), ref)
    except TreeReadError as exc:
        return {"error": exc.category, "message": str(exc)}, exc.status
    original = ({'state': 'unavailable', 'reason': 'cloud_snapshot'}
                if source['file_hash'] is not None else
                {'state': 'unavailable', 'reason': 'source_unresolvable'})
    leaf = object_value(result.get('leaf'))
    return {'company_id': company_id,
            'leaf': {'code': result.get('code'), 'trunk': leaf.get('trunk'),
                     'title': leaf.get('title'), 'version': result.get('version'),
                     'state': result.get('leaf_state')},
            'source': source, 'original': original}


def _matches(candidate, expected) -> bool:
    return bool(isinstance(candidate, str) and expected and
                hmac.compare_digest(candidate.encode("utf-8"), expected.encode("utf-8")))


def _as_list(value) -> list:
    return value if isinstance(value, list) else []


def normalize_knowledge(raw) -> dict:
    """Fill missing nested levels so the three-tab UI renders any old/partial snapshot.

    Defensive presentation defaulting only; it neither classifies fact values nor
    invents business data. Missing fields render as the template's empty states.
    """
    knowledge = object_value(raw)
    files = []
    for item in _as_list(object_value(knowledge.get("evidence")).get("files")):
        file = object_value(item)
        for level in ("l0", "l1", "l2"):
            layer = object_value(file.get(level))
            layer.setdefault("content", None)
            layer["source_ref"] = object_value(layer.get("source_ref"))
            file[level] = layer
        file["source_ref"] = object_value(file.get("source_ref"))
        file["warnings"] = _as_list(file.get("warnings"))
        files.append(file)

    facts = object_value(knowledge.get("facts"))
    groups = []
    for group in _as_list(facts.get("groups")):
        gdata = object_value(group)
        normalized_facts = []
        for fact in _as_list(gdata.get("facts")):
            fdata = object_value(fact)
            fdata["source_ref"] = object_value(fdata.get("source_ref"))
            normalized_facts.append(fdata)
        gdata["facts"] = normalized_facts
        groups.append(gdata)
    cap = object_value(facts.get("cap_table"))
    cap["warnings"] = _as_list(cap.get("warnings"))
    cap["events"] = _as_list(cap.get("events"))
    if cap.get("snapshot") is not None:
        snapshot = object_value(cap.get("snapshot"))
        snapshot["holders"] = object_value(snapshot.get("holders"))
        cap["snapshot"] = snapshot

    working = object_value(knowledge.get("working_memory"))
    working["warnings"] = _as_list(working.get("warnings"))
    items = []
    for item in _as_list(working.get("items")):
        idata = object_value(item)
        runtime = object_value(idata.get("runtime"))
        runtime["open_loops"] = _as_list(runtime.get("open_loops"))
        notes = [object_value(note) for note in _as_list(runtime.get("working_notes"))]
        runtime["working_notes"] = notes
        idata["runtime"] = runtime
        idata["events"] = _as_list(idata.get("events"))
        items.append(idata)

    return {"evidence": {"files": files},
            "facts": {"groups": groups, "cap_table": cap},
            "working_memory": {"items": items, "warnings": working["warnings"]}}


def normalize_source(raw) -> dict:
    """Ensure l2/pages exist for the L2 trace page on old/partial snapshots."""
    source = object_value(raw)
    l2 = object_value(source.get("l2"))
    l2["pages"] = _as_list(l2.get("pages"))
    l2.setdefault("mapping", None)
    l2.setdefault("manifest_uri", None)
    source["l2"] = l2
    source["warnings"] = _as_list(source.get("warnings"))
    return source


def _open_facts_tree_store(app, path):
    """Dedicated FactsTree snapshot store; a missing store fails reads closed.

    The host initializes this database explicitly (migrate --snapshot), just
    like the homepage store. The cloud window still boots without it so the
    unrelated read-only pages keep working; /api/facts/tree then reports the
    snapshot as unavailable instead of falling back to the homepage store.
    """
    try:
        return SnapshotStore(path)
    except RuntimeError:
        app.logger.warning('facts-tree snapshot store unavailable: %s', path)
        return None


def install_cloud_window(app, config, context_reader) -> None:
    app.config["MAX_CONTENT_LENGTH"] = MAX_BODY_BYTES
    app.extensions["sam_snapshot_store"] = SnapshotStore(config.data_root / "cloud-snapshot.db")
    app.extensions["sam_facts_tree_snapshot_store"] = _open_facts_tree_store(
        app, config.data_root / "cloud-facts-tree-snapshot.db")

    @app.context_processor
    def readonly_context():
        return {"read_only": True}

    @app.errorhandler(413)
    def oversized_body(_error):
        return {"error": "snapshot_too_large", "message": "快照超过 32 MiB 上限"}, 413

    @app.route('/healthz')
    def cloud_health():
        return {"ok": True, "read_only": True}

    @app.route('/ingest', methods=['POST'])
    def cloud_ingest():
        expected = os.environ.get('SAM_CLOUD_INGEST_KEY', '')
        if not expected.strip():
            return {"error": "ingest_not_configured", "message": "宿主未配置收数钥匙"}, 503
        if not _matches(request.headers.get('X-Ingest-Key'), expected):
            return {"error": "unauthorized", "message": "收数钥匙不正确"}, 401
        try:
            value = json.loads(request.get_data(), parse_constant=reject_constant)
            validate_snapshot(value)
            company = os.environ.get('SAM_COMPANY_ID', '').strip()
            if company and value.get('company_id') != company:
                raise ValueError('company_id does not match host scope')
            # Mapping internals are never persisted in the cloud.
            _project_knowledge(value)
            app.extensions['sam_snapshot_store'].replace(value)
        except (ValueError, UnicodeError, RecursionError, OverflowError) as exc:
            return {"error": "invalid_snapshot", "message": str(exc)}, 400
        except sqlite3.Error:
            app.logger.exception('cloud snapshot storage unavailable')
            return {"error": "storage_unavailable", "message": "快照存储暂不可用"}, 503
        return {"ok": True}

    @app.route('/ingest/facts-tree', methods=['POST'])
    def cloud_ingest_facts_tree():
        """Dedicated FactsTree snapshot intake; same key/host checks as /ingest.

        The payload must contain exactly the facts_tree section, and it is
        written only to the dedicated store, never to the homepage store.
        """
        expected = os.environ.get('SAM_CLOUD_INGEST_KEY', '')
        if not expected.strip():
            return {"error": "ingest_not_configured", "message": "宿主未配置收数钥匙"}, 503
        if not _matches(request.headers.get('X-Ingest-Key'), expected):
            return {"error": "unauthorized", "message": "收数钥匙不正确"}, 401
        try:
            value = json.loads(request.get_data(), parse_constant=reject_constant)
            validate_snapshot(value)
            company = os.environ.get('SAM_COMPANY_ID', '').strip()
            if company and value.get('company_id') != company:
                raise ValueError('company_id does not match host scope')
            payload = value['payload']
            if set(payload) != {'facts_tree'}:
                raise ValueError('payload must contain exactly the facts_tree section')
            section = payload['facts_tree']
            if not isinstance(section, dict) or section.get('company_id') != value['company_id']:
                raise ValueError('facts_tree company_id must match the envelope company_id')
            if section.get('state') == 'unbound':
                # DEC-020 option A: the unbound state carries no facts-tree
                # content at all — exactly the shape snapshot() emits. Tree
                # content in an unbound section is refused, never persisted.
                if set(section) != {'company_id', 'state'}:
                    raise ValueError('unbound facts_tree section must not carry tree content')
            else:
                # DEC-020 option A: only locally placement-validated snapshots
                # are accepted; the unbound state carries no tree content.
                from .facts_tree.tree_read import verify_placement_validation
                verify_placement_validation(section, value['company_id'])
            store = app.extensions.get('sam_facts_tree_snapshot_store')
            if store is None:
                return {"error": "storage_unavailable", "message": "快照存储暂不可用"}, 503
            store.replace(value)
        except (ValueError, UnicodeError, RecursionError, OverflowError) as exc:
            return {"error": "invalid_snapshot", "message": str(exc)}, 400
        except sqlite3.Error:
            app.logger.exception('facts-tree snapshot storage unavailable')
            return {"error": "storage_unavailable", "message": "快照存储暂不可用"}, 503
        return {"ok": True}

    @app.before_request
    def cloud_boundary():
        # Only authenticated ingestion can mutate anything in the cloud. This
        # covers future POST/PUT/PATCH/DELETE routes as well as today's routes.
        if request.endpoint in ('cloud_ingest', 'cloud_ingest_facts_tree') and request.method == 'POST':
            return None
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            return {"error": "cloud_readonly", "message": READONLY_MESSAGE}, 403
        if request.method == 'OPTIONS':
            return '', 204
        if request.endpoint == 'cloud_health':
            return None

        user = os.environ.get('SAM_VIEW_USER')
        password = os.environ.get('SAM_VIEW_PASS')
        if user is not None or password is not None:
            auth = request.authorization
            if not (auth and auth.type == 'basic' and _matches(auth.username, user)
                    and _matches(auth.password, password)):
                return 'Authentication required', 401, {'WWW-Authenticate': 'Basic realm="SAM cloud"'}
        if request.endpoint == 'static':
            return None
        # Auth still applies, but the shared home renderer needs no snapshot
        # or local services. Both modes use exactly the same visual source.
        if request.endpoint in ('home_page', 'prototype_page', 'knowledge_page', 'knowledge_source_page'):
            return None

        snapshot = current_snapshot(app)
        context = context_reader(company_id='')  # scope comes only from the stored envelope
        payload = object_value(object_value(snapshot).get('payload'))
        company_id = context['company_id']
        knowledge = SnapshotKnowledgeService(snapshot)
        data = knowledge.read(company_id=company_id)

        # These names are the existing public read API contracts, not business
        # rules. All bodies are opaque values taken from this one snapshot.
        if request.endpoint in ('api_business_overview',):
            return {'overview': payload.get('overview', {}), 'dashboard': payload.get('dashboard', {})}
        if request.endpoint == 'api_dashboard':
            return {'dashboard': payload.get('dashboard', {})}
        if request.endpoint == 'api_dashboard_preferences':
            return {'company_id': company_id, 'dashboard_id': 'business',
                    'hidden': payload.get('dashboard_preferences', [])}
        if request.endpoint == 'api_knowledge':
            # The local api_knowledge() view never runs behind this boundary,
            # so the redaction is applied here explicitly, not assumed.
            return mapping_redacted_projection(data)
        if request.endpoint == 'api_facts':
            return {'company_id': company_id, 'facts': context['facts']}
        if request.endpoint == 'api_facts_tree':
            return facts_tree_response(app)
        if request.endpoint == 'api_facts_tree_source':
            return facts_tree_source_response(app)
        if request.endpoint == 'api_todos':
            return {'company_id': company_id, 'todos': context['todos']}
        if request.endpoint == 'api_file_classifications':
            return {'company_id': company_id, 'files': context['files']}
        if request.endpoint == 'api_entity_roster':
            return {'company_id': company_id, 'entities': payload.get('entities', [])}
        if request.endpoint == 'api_parse_jobs':
            return {'jobs': payload.get('jobs', [])}
        if request.endpoint == 'api_parse_job':
            jobs = payload.get('jobs', [])
            job = next((item for item in jobs if isinstance(item, dict)
                        and item.get('job_id') == request.view_args['job_id']), None) if isinstance(jobs, list) else None
            return {'job': job} if job else ({'error': 'not_found'}, 404)
        if request.endpoint == 'api_import_guide_status':
            return {'status': 'idle', 'message': READONLY_MESSAGE}
        if request.endpoint == 'cloud_chat_history':
            return {'company_id': company_id, 'conversations': payload.get('conversations', [])}

        title = ''
        sections = []
        if request.endpoint == 'business_overview_page':
            title = '业务概览'
            sections = [('overview', payload.get('overview')), ('dashboard', payload.get('dashboard'))]
        elif request.endpoint == 'legacy_overview_page':
            title = '总览'
            sections = [('page_context', payload.get('page_context'))]
        elif request.endpoint == 'todos_page':
            from . import settings
            if settings.SAM_FACTS_TREE:
                from .facts_tree.todos import safe_md_view
                md = [safe_md_view(t, company_id=company_id) for t in context['todos']]
                md = [t for t in md if t is not None]
                if md:
                    # Only explicitly shaped read-only cards; never unpack a
                    # snapshot into template control variables or write controls.
                    legacy = []
                    for item in context['todos']:
                        if (not isinstance(item,dict) or item.get('company_id') != company_id
                                or item.get('reason') not in ('conflict','critical_review')): continue
                        view = {k:item.get(k) if isinstance(item.get(k),(str,int,float)) else ''
                            for k in ('metric','period','existing_value','candidate_value','candidate_unit','suggestion','source_file','source_page','status','reason')}
                        view['id'] = item.get('id') if type(item.get('id')) is int else 0
                        legacy.append(view)
                    return render_template('todos.html', company_id=company_id,
                        todos=md+legacy, md_conflicts=md,
                        open_conflicts=[t for t in legacy if t['reason']=='conflict'],
                        open_critical=[t for t in legacy if t['reason']=='critical_review'],
                        todo_status='open', readonly_todos=True,
                        facts=context['facts'], files=context['files'], visible_modules=context['visible_modules'])
            title = '待办中心'
            sections = [('todos', context['todos'])]
        elif request.endpoint == 'facts_page':
            # F-004 P03: render the same tree template as local; nodes come
            # from /api/facts/tree, so snapshot flat facts are never passed.
            # With no snapshot the same template still renders its empty state.
            return render_template('facts.html', company_id=company_id,
                                   todos=context['todos'])
        elif request.endpoint == 'detail':
            try:
                source = knowledge.source(company_id=company_id, file_hash=request.view_args['file_hash'])
            except KeyError:
                source = None
            title = '文件详情'
            sections = [('source', source)]
        elif request.endpoint == 'files_page':
            title = '文件柜'
            sections = [('files', context['files'])]
        elif request.endpoint == 'chat_page':
            title = '企业对话'
            sections = [('conversations', payload.get('conversations', []))]
        elif request.endpoint == 'inbox_page':
            title = '文件柜'
            sections = [('jobs', payload.get('jobs', []))]
        else:
            # Never fall through to a future local GET handler: it might
            # initialise a ledger, clean a runtime, or trigger an employee.
            abort(404)
        return render_template('cloud_page.html', **context, title=title, sections=sections,
                               snapshot=snapshot, snapshot_payload=payload)

    @app.route('/api/chat/history')
    def cloud_chat_history():
        # Response is supplied by cloud_boundary above, alongside other reads.
        raise AssertionError('cloud read boundary was bypassed')
