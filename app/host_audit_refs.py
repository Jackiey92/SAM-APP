"""Non-secret actor audit references for the SAM host (F-007 / CHG-068).

Approved design baseline 5e12baeaf2969d0de7d7c0b028e886a196b7d2ba,
CHG-068 section 5 (actor audit_ref v1 contract) under the approved
scheme A (DEC-031): the F-007 host ledger is authoritative for
identity, policy and approval state, and Git keeps only non-secret
audit references. This module implements the current-user and
root-task actor references on top of the existing OwnerContext and
TaskRunContext resolutions only:

- validate_actor_audit_ref(ref) is a pure structural check. It accepts
  exactly the two frozen shapes: the common fields (schema_version=1,
  audit_ref_type, installation_id, company_id, principal_id,
  member_revision) plus session_ref for user_actor, or task_id, run_id,
  policy_id and policy_revision for task_actor. Unknown fields, unknown
  or missing audit_ref_type, schema versions other than the builtin
  int 1, bool or int-subclass versions/revisions, non-dict input,
  non-string keys and invalid host-issued IDs are all refused. Approval
  references do not exist yet, so an approval_id field or any approval
  ref type is refused like every other unknown shape. The input is
  never modified, error messages never echo caller-supplied values,
  and the check performs no database, Git or model I/O.
- HostAuditReferenceService(identity, task_credentials) composes the
  existing services only. The constructor verifies the real service
  types and that both services share the very same sqlite connection
  object, that task_credentials.identity IS the given identity service
  and that its authorization ledger is bound to that same identity
  instance; cross-ledger assembly is refused before anything else. It
  runs no DDL, opens no database, reads no environment path and stores
  no state of its own.
- user_actor_ref / task_actor_ref re-resolve the real credential on
  EVERY call through HostIdentityService.resolve_session and
  HostTaskCredentialService.resolve_task_credential and return the
  exact plain dict. The secret only enters the real resolution entry
  point; it never reaches the result, an exception message, a repr or
  a log. There is no cache: a revoked session, revoked membership,
  revoked or expired credential, paused/staled policy or a stored
  scope that no longer fits its policy makes the very next call fail
  closed through the existing resolvers.
- verify_user_actor_ref / verify_task_actor_ref first run the pure
  structural validation, then re-resolve the corresponding credential
  live and compare every field exactly. Any mismatch is refused with a
  message that names the field but never prints either value. A
  caller-constructed OwnerContext/TaskRunContext is never accepted as
  a substitute for credential resolution: no such parameter exists.

A structurally valid or even currently matching audit_ref is NOT an
authorization: it proves nothing about an action, object, effect
digest, approval, budget or Git commit permission. The final
enforcement point must re-verify inside the Broker coordination
domain; a standalone read here is never a final commit permit, and a
historical reference that no longer validates does not delete history
or restore revoked authority.

Scope of this slice (explicitly NOT implemented here): one-time
approval ledgers and approval audit refs, operation/effect digest
binding, the Commit Broker, HTTP routes, feature gates, real company
data migration and any change to the global app.db schema or
migrations.
"""
from app.facts_tree import git_format
from app.host_identity import HostIdentityService
from app.host_task_credentials import HostTaskCredentialService

SCHEMA_VERSION = 1

USER_ACTOR_TYPE = "user_actor"
TASK_ACTOR_TYPE = "task_actor"

_COMMON_FIELDS = ("schema_version", "audit_ref_type", "installation_id",
                  "company_id", "principal_id", "member_revision")
_USER_FIELDS = _COMMON_FIELDS + ("session_ref",)
_TASK_FIELDS = _COMMON_FIELDS + ("task_id", "run_id", "policy_id",
                                 "policy_revision")


class HostAuditRefError(Exception):
    """Base class for every audit-reference failure; fails closed."""


class ActorAuditRefInvalid(HostAuditRefError):
    """The reference is structurally invalid; it is not audit_ref v1."""


class ActorAuditRefMismatch(HostAuditRefError):
    """A structurally valid reference does not match the current
    server-side state resolved from the real credential."""


def _check_id(field, value):
    # git_format.encode_id enforces the host-issued ID bounds: str,
    # non-empty, length-capped. Its messages never echo the value.
    try:
        git_format.encode_id(value)
    except (TypeError, ValueError):
        invalid = True
    else:
        invalid = False
    if invalid:
        # Raise only after the except suite has finished: no explicit
        # cause and no implicit context keeps the original exception,
        # whose object/repr can carry the caller-supplied id (e.g. one
        # with a lone surrogate that fails UTF-8 encoding).
        raise ActorAuditRefInvalid(
            "actor audit ref field %r is not a valid host-issued id"
            % field)


def _check_positive_int(field, value):
    # Only the exact builtin int is accepted: bool and int subclasses
    # are refused, per the frozen schema contract.
    if type(value) is not int or value < 1:
        raise ActorAuditRefInvalid(
            "actor audit ref field %r must be a positive builtin int; "
            "bool and int subclasses are refused" % field)


def validate_actor_audit_ref(ref):
    """Pure structural validation of one actor audit_ref v1.

    Returns None on success. Raises ActorAuditRefInvalid on any
    deviation from the two frozen shapes. Performs no database, Git or
    model I/O, never modifies the input and never prints caller-supplied
    values in error messages. A pass here is NOT an authorization and
    says nothing about whether the referenced identity is still valid.
    """
    if not isinstance(ref, dict):
        raise ActorAuditRefInvalid(
            "actor audit ref must be a dict of str keys")
    for key in ref:
        if not isinstance(key, str):
            raise ActorAuditRefInvalid(
                "actor audit ref keys must all be str")
    if "audit_ref_type" not in ref:
        raise ActorAuditRefInvalid(
            "actor audit ref is missing the audit_ref_type field")
    ref_type = ref["audit_ref_type"]
    if ref_type not in (USER_ACTOR_TYPE, TASK_ACTOR_TYPE) \
            or not isinstance(ref_type, str):
        # Unknown types include every approval reference shape: the
        # approval ledger does not exist yet, so none can be valid.
        raise ActorAuditRefInvalid(
            "actor audit ref has an unknown audit_ref_type")
    expected = frozenset(
        _USER_FIELDS if ref_type == USER_ACTOR_TYPE else _TASK_FIELDS)
    if frozenset(ref) != expected:
        raise ActorAuditRefInvalid(
            "actor audit ref fields do not exactly match the frozen "
            "%s schema; missing and unknown fields are both refused"
            % ref_type)
    if type(ref["schema_version"]) is not int \
            or ref["schema_version"] != SCHEMA_VERSION:
        raise ActorAuditRefInvalid(
            "actor audit ref schema_version must be the builtin int 1; "
            "bool and int subclasses are refused")
    _check_id("installation_id", ref["installation_id"])
    _check_id("company_id", ref["company_id"])
    _check_id("principal_id", ref["principal_id"])
    _check_positive_int("member_revision", ref["member_revision"])
    if ref_type == USER_ACTOR_TYPE:
        _check_id("session_ref", ref["session_ref"])
    else:
        _check_id("task_id", ref["task_id"])
        _check_id("run_id", ref["run_id"])
        _check_id("policy_id", ref["policy_id"])
        _check_positive_int("policy_revision", ref["policy_revision"])
    return None


def _compare(field, expected, actual):
    if expected != actual:
        raise ActorAuditRefMismatch(
            "actor audit ref field %r does not match the current "
            "server-side state" % field)


class HostAuditReferenceService:
    """Actor audit_ref v1 issuer/verifier on the existing F-007 stack.

    Composes the bound HostIdentityService and
    HostTaskCredentialService only; see the module docstring for the
    full contract. Creates no tables, opens no database, holds no
    cache and grants no authority.
    """

    def __init__(self, identity, task_credentials):
        if not isinstance(identity, HostIdentityService):
            raise TypeError(
                "host audit references require the bound "
                "HostIdentityService; caller-reported identity is "
                "never accepted")
        if not isinstance(task_credentials, HostTaskCredentialService):
            raise TypeError(
                "host audit references require the bound "
                "HostTaskCredentialService; caller-reported task state "
                "is never accepted")
        # Same-object checks, before anything else: both services must
        # share one connection object and one identity binding, so a
        # reference can never be assembled across divergent ledgers.
        # This constructor runs no DDL and no SQL at all.
        if task_credentials.identity is not identity:
            raise ValueError(
                "host audit references require the task credential "
                "service to be bound to the very same identity "
                "service; combining divergent identity instances is "
                "refused")
        if task_credentials.db is not identity.db:
            raise ValueError(
                "host audit references require both services to share "
                "the very same connection object; combining separate "
                "connections is refused")
        if task_credentials.authorization.identity is not identity:
            raise ValueError(
                "host audit references require the authorization "
                "ledger behind the task credential service to be bound "
                "to the very same identity service; combining "
                "divergent ledgers is refused")
        self.identity = identity
        self.task_credentials = task_credentials

    # -- reference generation -------------------------------------------------

    def user_actor_ref(self, session_secret, *, now):
        """Resolve the session live and return its exact audit_ref dict.

        Re-resolves through HostIdentityService.resolve_session on every
        call; there is no cache, so a revoked or expired session or a
        revoked membership fails closed on the very next call. The
        secret only enters the real resolution entry point and never
        appears in the result, in exceptions or in reprs. The returned
        plain dict carries no secret and grants nothing.
        """
        context = self.identity.resolve_session(session_secret, now=now)
        return {
            "schema_version": SCHEMA_VERSION,
            "audit_ref_type": USER_ACTOR_TYPE,
            "installation_id": context.installation_id,
            "company_id": context.company_id,
            "principal_id": context.principal_id,
            "member_revision": context.member_revision,
            "session_ref": context.session_ref,
        }

    def task_actor_ref(self, task_secret, *, now):
        """Resolve the task credential live and return its audit_ref dict.

        Re-resolves through HostTaskCredentialService.resolve_task_credential
        on every call; there is no cache, so a revoked or expired
        credential, a paused/staled policy, a widened stored scope or a
        revoked membership fails closed on the very next call. The
        secret only enters the real resolution entry point and never
        appears in the result, in exceptions or in reprs. The returned
        plain dict carries no secret and grants nothing.
        """
        context = self.task_credentials.resolve_task_credential(
            task_secret, now=now)
        return {
            "schema_version": SCHEMA_VERSION,
            "audit_ref_type": TASK_ACTOR_TYPE,
            "installation_id": context.installation_id,
            "company_id": context.company_id,
            "principal_id": context.principal_id,
            "member_revision": context.member_revision,
            "task_id": context.task_id,
            "run_id": context.run_id,
            "policy_id": context.policy_id,
            "policy_revision": context.policy_revision,
        }

    # -- verification -----------------------------------------------------------

    def verify_user_actor_ref(self, ref, session_secret, *, now):
        """Verify one user_actor ref against the live-resolved session.

        Structural validation first, then a fresh resolution of the
        real credential and an exact field-by-field comparison; any
        mismatch is refused without printing either value. Returns None
        on success. A caller-constructed OwnerContext is never accepted
        in place of credential resolution. A pass here is NOT an action
        authorization; the final enforcement point must re-verify.
        """
        validate_actor_audit_ref(ref)
        if ref["audit_ref_type"] != USER_ACTOR_TYPE:
            raise ActorAuditRefMismatch(
                "actor audit ref is not a user_actor reference")
        expected = self.user_actor_ref(session_secret, now=now)
        for field in _USER_FIELDS:
            _compare(field, expected[field], ref[field])
        return None

    def verify_task_actor_ref(self, ref, task_secret, *, now):
        """Verify one task_actor ref against the live-resolved credential.

        Structural validation first, then a fresh resolution of the
        real credential and an exact field-by-field comparison; any
        mismatch is refused without printing either value. Returns None
        on success. A caller-constructed TaskRunContext is never
        accepted in place of credential resolution. A pass here is NOT
        an action authorization; the final enforcement point must
        re-verify.
        """
        validate_actor_audit_ref(ref)
        if ref["audit_ref_type"] != TASK_ACTOR_TYPE:
            raise ActorAuditRefMismatch(
                "actor audit ref is not a task_actor reference")
        expected = self.task_actor_ref(task_secret, now=now)
        for field in _TASK_FIELDS:
            _compare(field, expected[field], ref[field])
        return None
