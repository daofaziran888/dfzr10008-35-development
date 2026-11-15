"""数据产品认证接口：按产品归属管控认证申请与审核，并回写产品发布准入。

权限口径：
* 只有产品负责人（同属产品归属单位）能提交认证申请，其他单位进入只读态；
* 审核只允许归属单位操作，跨单位审核直接拒绝，且不会改动已有审核结论；
* 申请时冻结认证标准、有效期限与标准符合性结果，认证材料为共享材料不可改动；
* 审核通过写入产品发布准入，驳回或过期立即移出可用认证集合。
"""
from __future__ import annotations

from typing import Any
from urllib.parse import unquote

from fastapi import APIRouter, HTTPException, Query, Request

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.badge import BadgeService, FROZEN_FIELDS

router = APIRouter(prefix="/api/badge", tags=["数据产品认证"])

service = BadgeService()

USER_HEADERS = ("x-operator", "x-user", "x-operator-name", "x-user-name")
UNIT_HEADERS = ("x-operator-unit", "x-user-unit", "x-unit")

LIST_FIELDS = [
    "认证编号",
    "产品编号",
    "产品名称",
    "所属单位",
    "认证类型",
    "认证标准",
    "认证机构",
    "有效期限",
    "认证结果",
    "认证状态",
    "申请人",
    "审核人",
    "发布准入",
    "可用",
]
STATUSES = ["申请中", "已通过", "已驳回", "已过期"]


def _header_value(request: Request | None, names: tuple[str, ...]) -> str:
    """读取身份请求头；兼容百分号编码与 latin-1 传输导致的中文乱码。"""
    if request is None:
        return ""
    for name in names:
        value = request.headers.get(name)
        if not value or not value.strip():
            continue
        value = value.strip()
        if "%" in value:
            value = unquote(value)
        try:
            # HTTP 头按 latin-1 解码，中文需要还原成 UTF-8；还原失败就按原值用。
            repaired = value.encode("latin-1").decode("utf-8")
            if repaired:
                value = repaired
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
        return value
    return ""


def _actor_from_request(
    request: Request,
    operator: str | None,
    unit: str | None,
    *,
    payload: EntryPayload | None = None,
    preferred_keys: tuple[str, ...] | None = None,
) -> dict[str, str]:
    """统一身份口径：查询参数 > 请求头 > values 字段，缺省时退回平台值班账号。"""
    values = payload.values if payload else {}
    keys = preferred_keys or ("操作人", "当前用户", "操作员", "operator", "actor", "user")
    return service.resolve_actor(
        operator=operator or _header_value(request, USER_HEADERS),
        unit=unit or _header_value(request, UNIT_HEADERS),
        values=values,
        preferred_keys=keys,
    )


@router.get("", response_model=PageResult[dict])
def list_entries(
    request: Request,
    keyword: str | None = Query(default=None, description="按认证编号、产品编号或认证标准检索"),
    status: str | None = Query(default=None, description="申请中、已通过、已驳回、已过期"),
    product_no: str | None = Query(default=None, description="按所属产品编号过滤"),
    standard: str | None = Query(default=None, description="按认证标准过滤"),
    only_available: bool = Query(default=False, description="只返回当前可用的认证集合"),
    operator: str | None = Query(default=None, description="当前操作人，非归属单位返回只读态"),
    unit: str | None = Query(default=None, description="当前操作人所属单位"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按认证编号、产品归属与状态过滤认证列表；非归属单位返回只读标记而不是隐藏数据。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    actor = service.resolve_actor(
        operator=operator or _header_value(request, USER_HEADERS),
        unit=unit or _header_value(request, UNIT_HEADERS),
    )
    items, total = service.list_entries(
        keyword=keyword,
        status=status,
        product_no=product_no,
        standard=standard,
        only_available=only_available,
        page=page,
        size=size,
        actor=actor,
    )
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/available", response_model=PageResult[dict])
def list_available(
    request: Request,
    product_no: str | None = Query(default=None, description="按所属产品编号过滤"),
    operator: str | None = Query(default=None),
    unit: str | None = Query(default=None),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """可用认证集合：只包含审核通过、标准符合性通过且在有效期内的记录。"""
    actor = service.resolve_actor(
        operator=operator or _header_value(request, USER_HEADERS),
        unit=unit or _header_value(request, UNIT_HEADERS),
    )
    rows = service.available_entries(product_no=product_no, actor=actor)
    total = len(rows)
    start = max(page - 1, 0) * size
    return PageResult(items=rows[start:start + size], total=total, page=page, size=size)


@router.get("/export")
def export_entries(
    request: Request,
    only_available: bool = False,
    operator: str | None = None,
    unit: str | None = None,
) -> dict[str, Any]:
    """导出认证清单：返回当前过滤条件下的全量数据。"""
    actor = service.resolve_actor(
        operator=operator or _header_value(request, USER_HEADERS),
        unit=unit or _header_value(request, UNIT_HEADERS),
    )
    items, total = service.list_entries(page=1, size=10000, only_available=only_available, actor=actor)
    return {"module": "badge", "total": total, "items": items, "冻结字段": list(FROZEN_FIELDS)}


@router.get("/{entry_id}", response_model=dict)
def get_entry(
    entry_id: int,
    request: Request,
    operator: str | None = None,
    unit: str | None = None,
) -> dict:
    """读取单条认证记录明细；非归属单位能看到数据但被标记为只读。"""
    actor = service.resolve_actor(
        operator=operator or _header_value(request, USER_HEADERS),
        unit=unit or _header_value(request, UNIT_HEADERS),
    )
    entry = service.get_entry(entry_id, actor=actor)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"认证记录 {entry_id} 不存在或已归档")
    return entry


@router.get("/{entry_id}/permissions", response_model=dict)
def get_permissions(
    entry_id: int,
    request: Request,
    operator: str | None = None,
    unit: str | None = None,
) -> dict:
    """查看当前操作人在该认证记录上的权限：是否可申请、可审核、是否只读。"""
    actor = service.resolve_actor(
        operator=operator or _header_value(request, USER_HEADERS),
        unit=unit or _header_value(request, UNIT_HEADERS),
    )
    entry = service.get_entry(entry_id, actor=actor)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"认证记录 {entry_id} 不存在或已归档")
    return entry["permissions"]


@router.post("", response_model=ActionResult)
def create_entry(
    payload: EntryPayload,
    request: Request,
    operator: str | None = Query(default=None, description="当前操作人"),
    unit: str | None = Query(default=None, description="当前操作人所属单位"),
) -> ActionResult:
    """登记认证申请：只有产品负责人可提交，非负责人或跨单位提交直接拒绝。"""
    actor = _actor_from_request(
        request,
        operator,
        unit,
        payload=payload,
        preferred_keys=("申请人", "操作人", "当前用户", "提交人"),
    )
    if not actor["姓名"]:
        return ActionResult(
            ok=False,
            message="缺少申请人：认证申请必须由产品负责人提交，非负责人只能查看认证记录",
        )
    entry, message, ok = service.create_entry(payload.values, actor=actor)
    if not ok:
        # 被拒绝时不回显整条记录，只提示已有审核结果的编号，避免误以为申请已登记。
        existing_no = str(entry.get("认证编号")) if entry is not None else ""
        if existing_no and existing_no not in message:
            message = f"{message}；已有认证审核结果：{existing_no}"
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.get("/{entry_id}/materials", response_model=dict)
def get_materials(entry_id: int) -> dict:
    """读取认证材料：标准核对表 STD-07、负责人签字与有效期区间。"""
    materials = service.materials_for(entry_id)
    if materials is None:
        raise HTTPException(status_code=404, detail=f"认证记录 {entry_id} 不存在或已归档")
    return materials


@router.put("/{entry_id}/materials", response_model=ActionResult)
def replace_materials(entry_id: int, payload: EntryPayload) -> ActionResult:
    """修改认证材料（PUT 写法）：共享材料在申请时冻结，一律拒绝并保留原值。"""
    return update_materials(entry_id, payload)


@router.post("/{entry_id}/materials", response_model=ActionResult)
def update_materials(entry_id: int, payload: EntryPayload) -> ActionResult:
    """修改认证材料：共享材料在申请时冻结，一律拒绝并保留原值。"""
    entry, message, ok = service.update_materials(entry_id, payload.values)
    if entry is None:
        return ActionResult(ok=False, message=message)
    # 拒绝时不回显材料内容，只说明拒绝原因。
    return ActionResult(ok=ok, message=message)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(
    entry_id: int,
    payload: EntryPayload,
    request: Request,
    operator: str | None = Query(default=None, description="当前操作人"),
    unit: str | None = Query(default=None, description="当前操作人所属单位"),
) -> ActionResult:
    """审核认证：跨单位审核拒绝、冻结字段不可改、材料不可改，结论回写产品发布准入。"""
    action = str(payload.values.get("action") or "").strip()
    actor = _actor_from_request(
        request,
        operator,
        unit,
        payload=payload,
        preferred_keys=("审核人", "操作人", "当前用户", "审核员"),
    )
    entry, message, ok = service.run_action(
        entry_id,
        action,
        actor=actor,
        changes=payload.values,
    )
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=ok, message=message, entry=entry)


@router.get("/{entry_id}/admission", response_model=dict)
def get_admission(entry_id: int) -> dict:
    """查看该认证记录对应的产品发布准入快照；驳回或过期后这里读不到认证。"""
    admission = service.admission_for_badge(entry_id)
    if admission is None:
        raise HTTPException(status_code=404, detail=f"认证记录 {entry_id} 不存在或已归档")
    return admission

