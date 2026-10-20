"""Product ingestion seam after conversion/QC; no original-binary access."""
import json
import os

from .dossier import Dossier
from .execution import run_files
from .service import DirectReadService
from ..db.database import connect
from ..parse_jobs import ParseJobPartial


def phase_limits(env=None):
    env = os.environ if env is None else env
    try: limits = json.loads(env['SAM_MD_PHASE_LIMITS'])
    except (KeyError, ValueError) as exc:
        raise ValueError('请明确配置 SAM_MD_PHASE_LIMITS 的五阶段累计调用限额') from exc
    phases = {'classify','attribute','extract','plan','leaf'}
    if not isinstance(limits,dict) or set(limits)!=phases or any(type(v) is not int or not 0<=v<=10000 for v in limits.values()):
        raise ValueError('SAM_MD_PHASE_LIMITS must contain five integer totals in 0..10000')
    return limits


def v4_phase_limits(env=None):
    env = os.environ if env is None else env
    try: limits = json.loads(env['SAM_MD_V4_PHASE_LIMITS'])
    except (KeyError, ValueError) as exc:
        raise ValueError('明确选择 V4 的上传必须配置 SAM_MD_V4_PHASE_LIMITS 的 review/publish 两阶段累计调用限额') from exc
    phases = {'review','publish'}
    if not isinstance(limits,dict) or set(limits)!=phases or any(type(v) is not int or not 0<=v<=10000 for v in limits.values()):
        raise ValueError('SAM_MD_V4_PHASE_LIMITS must contain exactly review and publish integer totals in 0..10000')
    return limits


def run_mapped_upload(*, job, manifest, config, memory, cancel, progress, model=None):
    limits = phase_limits()
    notes_v4 = DirectReadService._notes_protocol_selected()
    if notes_v4:
        # An explicitly selected V4 upload may only report success after the
        # post-read review and fact publication finish. The dedicated two-phase
        # budget is validated here, before any employee call; a missing or
        # malformed value fails closed. V3 ignores this V4-only budget and
        # keeps its five-phase behavior unchanged.
        limits = {**limits, **v4_phase_limits()}
    if manifest.get('file_hash') != job['file_hash']: raise ValueError('mapped source differs from job')
    dossier = Dossier.from_quality(manifest['document'], file_hash=job['file_hash'], objects_root=config.objects_dir,
        company_id=job['company_id'], job_id=job['job_id'], quality=manifest.get('document_quality'))
    with connect(config.main_db) as db:
        row = db.execute('SELECT content FROM memory_kv WHERE uri=?', ('sam://kv/rt/md-upload-options/'+job['job_id'],)).fetchone()
    options = json.loads(row[0]) if row else {}
    progress(stage='reading', message='正在分段阅读资料并保留出处')
    reader = DirectReadService(config.main_db, config=config, dossier=dossier, memory=memory, model=model)
    # ParseJobManager supplies the bounded multi-file executor; this same host
    # entry is also usable via U2 run_files for explicitly selected jobs.
    result = run_files([dict(reader_factory=lambda:reader, company_id=job['company_id'], job_id=job['job_id'],
        limits=limits, cancel=cancel, correction_attempts=int(os.environ.get('SAM_MD_CORRECTION_ATTEMPTS','2')),
        resume_unresolved=options.get('resume_unresolved',False),
        renew_deadline_seconds=options.get('renew_deadline_seconds'),retry_request_id=options.get('request_id'))], workers=1)[0]
    if isinstance(result,Exception): raise result
    notes_progress = result.get('mapped_notes')
    if notes_progress is not None:
        if (not result.get('done') or notes_progress.get('technical_rejections')
                or notes_progress.get('unresolved_readings')):
            raise ParseJobPartial('已保留整份阅读笔记进度；调用限额、期限或资料校验尚未完成，可明确续跑。')
        publication = notes_progress.get('publication')
        if (notes_progress.get('review_complete') is not True
                or notes_progress.get('open_notes',1) != 0
                or not isinstance(publication,dict) or publication.get('complete') is not True):
            raise ParseJobPartial('已保留整份阅读、笔记与复核进度；读后复核未完成、尚有未处理笔记或事实发布未完成，可明确续跑。')
        progress(stage='reading', message='整份资料阅读、读后复核与事实发布完成')
        return result
    if (not result.get('done') or result.get('mapped_md',{}).get('technical_rejections')
            or result.get('mapped_md',{}).get('unresolved_readings')):
        raise ParseJobPartial('已保留处理进度；调用限额、期限或资料校验尚未完成，可明确续跑。')
    progress(stage='reading',message=f"资料处理完成；{result['mapped_md'].get('pending_todos',0)} 项内容待经理审核")
    return result
