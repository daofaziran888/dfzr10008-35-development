"""数据产品认证业务规则：产品归属权限、申请冻结、审核回写与过期清理都收在这里。

认证围绕产品归属运转：

* 只有产品负责人（且属于产品归属单位）能提交认证申请，其他单位只能查看；
* 申请一旦提交，认证标准、有效期限与标准符合性结果即被冻结，后续不能改写；
* 审核只能由归属单位完成，跨单位审核会被拒绝，并且不会覆盖已有审核结论；
* 审核通过后把结论与有效期回写到产品的发布准入；驳回或过期则立即撤下准入；
* 同一产品的同一认证标准只保留一条认证审核结果。
"""
from __future__ import annotations

import calendar
import re
from datetime import date
from typing import Any

from app.store import store

MODULE = "badge"
PRODUCT_MODULE = "product"

REQUIRED_FIELDS = ["认证编号", "产品名称", "认证类型"]
STATUS_ORDER = ["申请中", "已通过", "已驳回", "已过期"]

# 申请提交后不允许再改写的字段；审核动作也不能借修改名义动这些值。
FROZEN_FIELDS = ["认证标准", "有效期限", "有效期起", "有效期至", "标准符合性结果", "申请时标准符合性结果"]

REVIEW_ACTIONS = {"通过认证", "审核通过", "通过审核", "认证通过", "批准认证", "核准认证"}
REJECT_ACTIONS = {"驳回认证", "审核驳回", "驳回审核", "认证驳回", "拒绝认证"}
EXPIRE_ACTIONS = {"过期认证", "标记过期", "置为过期"}
EXTEND_ACTIONS = {"延期认证", "续期认证", "延长有效期"}
APPLY_ACTIONS = {"提交认证", "提交申请", "申请认证"}
ACTION_RULES = {
    **{action: "已通过" for action in REVIEW_ACTIONS},
    **{action: "已驳回" for action in REJECT_ACTIONS},
    **{action: "已过期" for action in EXPIRE_ACTIONS},
    **{action: "已通过" for action in EXTEND_ACTIONS},
    **{action: "申请中" for action in APPLY_ACTIONS},
}
NEGATIVE_ACTIONS = REJECT_ACTIONS | EXPIRE_ACTIONS

# 平台侧的账号目录：真实项目会来自统一身份服务，这里用于把姓名映射到归属单位。
USERS: dict[str, dict[str, str]] = {
    "周宁": {"姓名": "周宁", "单位": "华东渠道组", "角色": "产品负责人"},
    "李敏": {"姓名": "李敏", "单位": "华东渠道组", "角色": "认证审核员"},
    "王芳": {"姓名": "王芳", "单位": "华东渠道组", "角色": "产品负责人"},
    "赵磊": {"姓名": "赵磊", "单位": "华北渠道组", "角色": "产品负责人"},
    "陈然": {"姓名": "陈然", "单位": "华南渠道组", "角色": "渠道运营"},
    "值班管理员": {"姓名": "值班管理员", "单位": "平台运营中心", "角色": "平台运营"},
}
DEFAULT_USER = {"姓名": "值班管理员", "单位": "平台运营中心", "角色": "平台运营"}
REVIEWER_ROLE = "认证审核员"

PRODUCT_NUMBER_KEYS = ("产品编号", "产品ID", "产品ID编号", "product_no", "productNo")
STANDARD_KEYS = ("认证标准", "标准编号", "认证标准编号", "standard")
VALIDITY_KEYS = ("有效期限", "有效期至", "认证有效期", "valid_until")
VALIDITY_START_KEYS = ("有效期起", "生效日期", "valid_from")
APPLICANT_KEYS = ("申请人", "申请负责人", "提交人", "applicant")
OPERATOR_KEYS = ("操作人", "当前用户", "操作员", "operator", "actor", "user")
REVIEWER_KEYS = ("审核人", "审核员", "reviewer")
MATERIAL_KEYS = ("认证材料", "材料", "审核材料", "materials")

_DATE_RE = re.compile(r"(\d{4})[-/.年]\s*(\d{1,2})(?:[-/.月]\s*(\d{1,2}))?")


def _first(values: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = values.get(key)
        if value is not None and str(value).strip():
            return value
    return None


def _clean(value: Any) -> str:
    return str(value).strip() if value is not None else ""


class BadgeService:
    # ---------------------------------------------------------------- 基础读取
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        product_no: str | None = None,
        standard: str | None = None,
        only_available: bool = False,
        page: int = 1,
        size: int = 20,
        actor: dict[str, str] | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        self.refresh_expiry()
        rows = store.rows(MODULE)
        if keyword:
            keyword = keyword.strip()
            rows = [
                row
                for row in rows
                if keyword in " ".join(
                    _clean(row.get(field))
                    for field in ("认证编号", "产品编号", "产品名称", "认证标准", "申请人", "负责人")
                )
            ]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        if product_no:
            rows = [row for row in rows if row.get("产品编号") == product_no]
        if standard:
            rows = [row for row in rows if _clean(row.get("认证标准")) == standard.strip()]
        if only_available:
            rows = [row for row in rows if self.is_available(row)]
        total = len(rows)
        start = max(page - 1, 0) * size
        items = [self.decorate(row, actor) for row in rows[start:start + size]]
        return items, total

    def get_entry(self, entry_id: int, actor: dict[str, str] | None = None) -> dict[str, Any] | None:
        self.refresh_expiry()
        row = store.find(MODULE, entry_id)
        return self.decorate(row, actor) if row else None

    def decorate(self, row: dict[str, Any], actor: dict[str, str] | None) -> dict[str, Any]:
        """返回副本，并补上当前操作人视角下的可用状态与权限位。"""
        entry = dict(row)
        entry["可用"] = self.is_available(row)
        entry["available"] = entry["可用"]
        entry["只读"] = self.is_readonly(row, actor)
        entry["readonly"] = entry["只读"]
        entry["permissions"] = self.permissions_for(row, actor)
        return entry

    # ---------------------------------------------------------------- 身份与归属
    def resolve_actor(
        self,
        *,
        operator: str | None = None,
        unit: str | None = None,
        values: dict[str, Any] | None = None,
        preferred_keys: tuple[str, ...] = OPERATOR_KEYS,
    ) -> dict[str, str]:
        """把请求里的姓名/单位解析成操作人；姓名可查账号目录得到单位。"""
        values = values or {}
        name = _clean(operator)
        if not name:
            name = _clean(_first(values, preferred_keys))
        if not name:
            # 动作接口里可能只写了「申请人」或「审核人」，按调用场景取一个。
            name = _clean(_first(values, APPLICANT_KEYS + REVIEWER_KEYS))
        if not name:
            return {"姓名": "", "单位": _clean(unit), "角色": ""}
        profile = USERS.get(name, {})
        resolved_unit = _clean(unit) or _clean(values.get("操作单位")) or _clean(values.get("所属单位")) \
            or _clean(values.get("申请单位")) or profile.get("单位", "")
        return {
            "姓名": name,
            "单位": resolved_unit,
            "角色": profile.get("角色", ""),
        }

    def product_by_number(self, product_no: str) -> dict[str, Any] | None:
        for row in store.rows(PRODUCT_MODULE):
            if _clean(row.get("产品编号")) == product_no:
                return row
        return None

    def product_by_name(self, product_name: str) -> dict[str, Any] | None:
        for row in store.rows(PRODUCT_MODULE):
            if _clean(row.get("产品名称")) == product_name:
                return row
        return None

    def owner_of(self, product: dict[str, Any] | None) -> str:
        return _clean(product.get("负责人")) if product else ""

    def unit_of_product(self, product: dict[str, Any] | None) -> str:
        if not product:
            return ""
        unit = _clean(product.get("所属单位")) or _clean(product.get("归属单位"))
        if unit:
            return unit
        return _clean(USERS.get(self.owner_of(product), {}).get("单位"))

    def _resolve_product(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        product_no = _clean(_first(values, PRODUCT_NUMBER_KEYS))
        product = self.product_by_number(product_no) if product_no else None
        if product is None:
            product_name = _clean(values.get("产品名称"))
            product = self.product_by_name(product_name) if product_name else None
        if product is None:
            if product_no:
                return None, f"产品 {product_no} 不存在，无法校验认证申请的归属单位"
            return None, "缺少产品归属信息：请填写已登记的产品编号"
        return product, ""

    def is_readonly(self, row: dict[str, Any], actor: dict[str, str] | None) -> bool:
        actor = actor or {"姓名": "", "单位": ""}
        if not actor.get("姓名"):
            # 没有明确操作人时不做越权判定，避免把匿名读取误标成只读；
            # 页面与动作接口都会带上操作人。
            return False
        product = self._product_for_entry(row)
        owner = self.owner_of(product)
        unit = self.unit_of_product(product)
        same_unit = bool(unit) and actor.get("单位") == unit
        is_owner = bool(owner) and actor.get("姓名") == owner
        return not (same_unit or is_owner)

    def permissions_for(self, row: dict[str, Any], actor: dict[str, str] | None) -> dict[str, Any]:
        actor = actor or {"姓名": "", "单位": "", "角色": ""}
        product = self._product_for_entry(row)
        owner = self.owner_of(product)
        unit = self.unit_of_product(product)
        is_owner = bool(actor.get("姓名")) and actor.get("姓名") == owner
        same_unit = bool(unit) and actor.get("单位") == unit
        status = _clean(row.get("status"))
        return {
            "产品编号": _clean(row.get("产品编号")) or _clean(product.get("产品编号") if product else ""),
            "负责人": owner,
            "归属单位": unit,
            "操作人": actor.get("姓名", ""),
            "操作人单位": actor.get("单位", ""),
            "can_apply": is_owner and same_unit and status in ("申请中", "已驳回", "已过期"),
            "can_review": same_unit and status in ("申请中", "已通过", "已过期"),
            "can_modify_materials": False,
            "readonly": self.is_readonly(row, actor),
        }

    def _product_for_entry(self, row: dict[str, Any]) -> dict[str, Any] | None:
        product_no = _clean(row.get("产品编号"))
        product = self.product_by_number(product_no) if product_no else None
        if product is None and row.get("产品名称"):
            product = self.product_by_name(_clean(row.get("产品名称")))
        return product

    # ---------------------------------------------------------------- 申请登记
    def create_entry(
        self,
        values: dict[str, Any],
        actor: dict[str, str] | None = None,
    ) -> tuple[dict[str, Any] | None, str, bool]:
        self.refresh_expiry()
        product, product_error = self._resolve_product(values)
        if product is None:
            return None, product_error, False

        product_no = _clean(product.get("产品编号"))
        product_name = _clean(product.get("产品名称"))
        owner = self.owner_of(product)
        product_unit = self.unit_of_product(product)

        applicant = actor or {"姓名": "", "单位": "", "角色": ""}
        applicant_name = _clean(applicant.get("姓名")) or _clean(_first(values, APPLICANT_KEYS))
        applicant_unit = _clean(applicant.get("单位")) or _clean(values.get("申请单位")) \
            or _clean(USERS.get(applicant_name, {}).get("单位"))
        if not applicant_name:
            applicant_name = owner
        if not applicant_name:
            return None, "缺少申请人：认证申请必须由产品负责人提交", False

        # 归属校验：非负责人、跨单位都直接拒绝，且不落任何记录。
        if applicant_name != owner:
            return None, f"认证申请被拒绝：{applicant_name} 不是产品 {product_no} 的负责人（负责人为 {owner}），仅负责人可提交认证", False
        if not applicant_unit:
            applicant_unit = product_unit
        if product_unit and applicant_unit and applicant_unit != product_unit:
            return None, f"认证申请被拒绝：申请人所属单位「{applicant_unit}」与产品归属单位「{product_unit}」不一致，跨单位提交不允许", False

        standard = _clean(_first(values, STANDARD_KEYS))
        if not standard:
            return None, "认证申请缺少认证标准，无法冻结审核口径", False
        validity = _first(values, VALIDITY_KEYS)
        if not _clean(validity):
            return None, "认证申请缺少有效期限，无法冻结审核口径", False
        start_date, end_date = self._parse_validity(values)
        if end_date is None:
            return None, f"有效期限「{_clean(validity)}」无法解析，请使用 YYYY-MM-DD 或 YYYY-MM", False

        # 同一产品 + 同一认证标准只保留一个审核结果，重复提交不再新增记录。
        existing = self.find_same_standard(product_no, standard)
        if existing is not None:
            return existing, (
                f"产品 {product_no} 的认证标准「{standard}」已有认证审核结果"
                f"（{_clean(existing.get('认证编号'))}，状态：{_clean(existing.get('status'))}），不重复登记"
            ), False

        material_values = values.get("认证材料") or values.get("材料") or values.get("审核材料") or {}
        materials = self._build_materials(applicant_name, start_date, end_date, material_values, values)
        missing_materials = [name for name, content in materials.items() if not _clean(content)]
        if missing_materials:
            return None, f"认证材料不完整：缺少{'、'.join(missing_materials)}", False
        if "STD-07" not in _clean(materials.get("标准核对表")):
            return None, "认证材料不完整：标准核对表编号必须为 STD-07", False

        rows = store.rows(MODULE)
        entry_id = max((int(row.get("id", 0)) for row in rows), default=0) + 1
        badge_no = _clean(values.get("认证编号")) or self._next_badge_no()
        if any(_clean(row.get("认证编号")) == badge_no for row in rows):
            return None, f"认证编号 {badge_no} 已存在，请更换编号", False

        today = self.today_iso()
        interval_text = self._interval_text(start_date, end_date)
        entry: dict[str, Any] = {
            "id": entry_id,
            "status": "申请中",
            "pending": True,
            "abnormal": False,
            "认证编号": badge_no,
            "产品编号": product_no,
            "产品名称": product_name,
            "所属单位": product_unit,
            "认证类型": _clean(values.get("认证类型")) or "数据产品认证",
            "认证标准": standard,
            "认证机构": _clean(values.get("认证机构")) or f"{product_unit or '产品归属单位'}认证审核组",
            "有效期限": _clean(validity),
            "有效期起": start_date.isoformat() if start_date else "",
            "有效期至": end_date.isoformat(),
            "有效期区间": interval_text,
            "认证结果": "待审核",
            "认证状态": "申请中",
            "标准符合性结果": "待审核",
            "申请时标准符合性结果": _clean(values.get("申请时标准符合性结果")) or "符合（材料核对通过）",
            "申请人": applicant_name,
            "申请单位": applicant_unit,
            "负责人": owner,
            "申请时间": today,
            "冻结": True,
            "冻结字段": list(FROZEN_FIELDS),
            "冻结时间": today,
            "审核人": "",
            "审核单位": "",
            "审核时间": "",
            "审核结论": "",
            "认证材料": materials,
            "材料清单": self._material_list(materials),
            "材料冻结": True,
            "可用": False,
            "available": False,
            "发布准入": "未准入",
        }
        rows.append(entry)
        self._sync_product_admission(entry, product, admitted=False)
        return self.decorate(entry, actor), "认证申请已登记，认证标准、有效期限与标准符合性结果已冻结", True

    def find_same_standard(self, product_no: str, standard: str) -> dict[str, Any] | None:
        for row in store.rows(MODULE):
            if _clean(row.get("产品编号")) == product_no and _clean(row.get("认证标准")) == standard.strip():
                return row
        return None

    def _next_badge_no(self) -> str:
        biggest = 0
        for row in store.rows(MODULE):
            match = re.search(r"(\d+)$", _clean(row.get("认证编号")))
            if match:
                biggest = max(biggest, int(match.group(1)))
        return f"BADGE-{biggest + 1:04d}"

    def _build_materials(
        self,
        applicant: str,
        start_date: date | None,
        end_date: date,
        material_values: Any,
        values: dict[str, Any],
    ) -> dict[str, str]:
        interval = self._interval_text(start_date, end_date)
        if isinstance(material_values, dict):
            base = {str(key): _clean(value) for key, value in material_values.items()}
        elif isinstance(material_values, list):
            base = {f"材料{index + 1}": _clean(item) for index, item in enumerate(material_values)}
        else:
            base = {"材料说明": _clean(material_values)} if _clean(material_values) else {}

        checklist = _clean(values.get("标准核对表")) or _clean(base.get("标准核对表")) or "STD-07"
        signature = _clean(values.get("负责人签字")) or _clean(base.get("负责人签字")) or applicant
        materials = {
            "标准核对表": checklist if checklist.startswith("标准核对表") else f"标准核对表 {checklist}",
            "负责人签字": signature if signature.startswith("负责人签字") else f"负责人签字：{signature}",
            "有效期": interval if ":" in interval or "：" in interval else f"有效期：{interval}",
        }
        for name, content in base.items():
            materials.setdefault(name, content)
        return materials

    def _material_list(self, materials: dict[str, str]) -> list[str]:
        return [_clean(value) for value in materials.values() if _clean(value)]

    # ---------------------------------------------------------------- 审核与动作
    def run_action(
        self,
        entry_id: int,
        action: str,
        actor: dict[str, str] | None = None,
        changes: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, str, bool]:
        self.refresh_expiry()
        changes = changes or {}
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"认证记录 {entry_id} 不存在或已归档", False
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于数据产品认证可执行范围", False

        # 冻结字段不能被动作请求顺手改写，共享材料也不能在审核阶段改。
        # 延期是唯一有明确入口调整有效期的动作，提交时走延期分支单独校验。
        protected_fields = [field for field in FROZEN_FIELDS if action not in EXTEND_ACTIONS or field not in ("有效期限", "有效期至")]
        frozen_changed = [
            field
            for field in protected_fields
            if field in changes and _clean(changes.get(field)) != _clean(entry.get(field))
        ]
        if frozen_changed:
            return self.decorate(entry, actor), (
                f"认证标准、有效期限与标准符合性结果已在申请时冻结，不能修改：{'、'.join(frozen_changed)}"
            ), False
        if any(key in changes and changes.get(key) not in (None, "", {}, []) for key in MATERIAL_KEYS):
            return self.decorate(entry, actor), "认证材料为申请时冻结的共享材料，审核阶段不能改动", False

        product = self._product_for_entry(entry)
        owner = self.owner_of(product)
        product_unit = self.unit_of_product(product)

        # 提交认证：等同于重新登记，只允许负责人补提，且不会绕过唯一标准约束。
        if action in APPLY_ACTIONS:
            if entry.get("status") != "申请中":
                return self.decorate(entry, actor), f"认证记录当前状态为「{_clean(entry.get('status'))}」，不能重复提交", False
            return self.decorate(entry, actor), "认证申请已在审核队列中，无需重复提交", True

        reviewer = actor or {"姓名": "", "单位": "", "角色": ""}
        reviewer_name = _clean(reviewer.get("姓名")) or _clean(_first(changes, REVIEWER_KEYS))
        reviewer_unit = _clean(reviewer.get("单位")) or _clean(changes.get("审核单位")) \
            or _clean(USERS.get(reviewer_name, {}).get("单位"))
        if not reviewer_name:
            reviewer_name = self._default_reviewer(product_unit, owner)
        if not reviewer_unit:
            reviewer_unit = _clean(USERS.get(reviewer_name, {}).get("单位"))

        # 跨单位审核直接拒绝，并保留当时的认证结论、状态和产品准入。
        if reviewer_unit and product_unit and reviewer_unit != product_unit:
            return self.decorate(entry, actor), (
                f"审核被拒绝：审核人 {reviewer_name} 属于「{reviewer_unit}」，"
                f"产品 {_clean(entry.get('产品编号'))} 归属「{product_unit}」，不允许跨单位审核；"
                f"原审核结论「{_clean(entry.get('审核结论')) or '未审核'}」保留不变"
            ), False

        target_status = ACTION_RULES[action]
        if action in EXTEND_ACTIONS and entry.get("status") not in ("已通过", "已过期"):
            return self.decorate(entry, actor), "只有已通过或已过期的认证可以延期", False

        if action in REVIEW_ACTIONS:
            if entry.get("status") == "已驳回":
                return self.decorate(entry, actor), "该认证已被驳回，审核结论保留，不能直接改判为通过", False
            if entry.get("status") == "已过期":
                return self.decorate(entry, actor), "该认证已过期并移出可用认证集合，需重新提交认证或走延期流程", False
            entry["status"] = "已通过"
            entry["pending"] = False
            entry["abnormal"] = False
            entry["认证状态"] = "已通过"
            entry["认证结果"] = "通过"
            entry["审核结论"] = "通过"
            entry["标准符合性结果"] = "通过"
            entry["审核人"] = reviewer_name
            entry["审核单位"] = reviewer_unit or product_unit
            entry["审核时间"] = self.today_iso()
            entry["可用"] = True
            entry["available"] = True
            entry["发布准入"] = "已准入"
            if product is not None:
                self._sync_product_admission(entry, product, admitted=True)
            message = f"认证审核通过，标准符合性结果已置为通过，并回写产品 {_clean(entry.get('产品编号'))} 的发布准入"

        elif action in REJECT_ACTIONS:
            entry["status"] = "已驳回"
            entry["pending"] = False
            entry["abnormal"] = True
            entry["认证状态"] = "已驳回"
            entry["认证结果"] = "驳回"
            entry["审核结论"] = "驳回"
            entry["审核人"] = reviewer_name
            entry["审核单位"] = reviewer_unit or product_unit
            entry["审核时间"] = self.today_iso()
            entry["可用"] = False
            entry["available"] = False
            entry["发布准入"] = "未准入"
            self._sync_product_admission(entry, product, admitted=False)
            message = f"认证已驳回，已立即移出可用认证集合，并撤下产品 {_clean(entry.get('产品编号'))} 的发布准入"

        elif action in EXPIRE_ACTIONS:
            self._mark_expired(entry, reason="审核动作标记过期")
            message = f"认证已过期，已立即移出可用认证集合，并撤下产品 {_clean(entry.get('产品编号'))} 的发布准入"

        elif action in EXTEND_ACTIONS:
            extended_text = _clean(changes.get("有效期至")) or _clean(changes.get("有效期限")) \
                or _clean(changes.get("延期至"))
            if not extended_text:
                return self.decorate(entry, actor), "延期认证需要提供新的有效期至", False
            start_date, end_date = self._parse_validity({"有效期至": extended_text})
            if end_date is None:
                return self.decorate(entry, actor), f"新的有效期「{extended_text}」无法解析", False
            if end_date < date.today():
                return self.decorate(entry, actor), "新的有效期已经过期，不能延期", False
            original_end_text = _clean(entry.get("有效期至")) or _clean(entry.get("有效期限"))
            original_end = self._parse_date(original_end_text, end_of_month=True)
            if original_end is not None and end_date <= original_end:
                return self.decorate(entry, actor), f"新有效期 {end_date.isoformat()} 不晚于当前有效期 {original_end.isoformat()}，无需延期", False
            history = entry.setdefault("延期记录", [])
            history.append({
                "原有效期至": _clean(entry.get("有效期至")),
                "新有效期至": end_date.isoformat(),
                "延期人": reviewer_name,
                "延期时间": self.today_iso(),
            })
            entry["有效期至"] = end_date.isoformat()
            entry["有效期限"] = _clean(changes.get("有效期限")) or end_date.isoformat()
            entry["有效期区间"] = self._interval_text(start_date, end_date)
            entry["status"] = "已通过"
            entry["pending"] = False
            entry["abnormal"] = False
            entry["认证状态"] = "已通过"
            entry["认证结果"] = "通过"
            entry["审核结论"] = "通过（延期）"
            entry["审核人"] = reviewer_name
            entry["审核单位"] = reviewer_unit or product_unit
            entry["审核时间"] = self.today_iso()
            entry["可用"] = True
            entry["available"] = True
            entry["发布准入"] = "已准入"
            if product is not None:
                self._sync_product_admission(entry, product, admitted=True)
            message = f"认证已延期至 {end_date.isoformat()}，并更新产品发布准入"

        else:  # pragma: no cover - ACTION_RULES 已覆盖全部已知动作
            entry["status"] = target_status
            message = f"认证记录已{action}"

        return self.decorate(entry, actor), message, True

    def _default_reviewer(self, product_unit: str, owner: str) -> str:
        for profile in USERS.values():
            if profile.get("单位") == product_unit and profile.get("角色") == REVIEWER_ROLE:
                return profile["姓名"]
        return owner

    # ---------------------------------------------------------------- 材料与冻结
    def materials_for(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None
        return {
            "认证编号": _clean(entry.get("认证编号")),
            "产品编号": _clean(entry.get("产品编号")),
            "材料冻结": bool(entry.get("材料冻结")),
            "冻结字段": list(entry.get("冻结字段") or []),
            "认证材料": dict(entry.get("认证材料") or {}),
            "材料清单": list(entry.get("材料清单") or []),
            "标准核对表": _clean((entry.get("认证材料") or {}).get("标准核对表")),
            "负责人签字": _clean((entry.get("认证材料") or {}).get("负责人签字")),
            "有效期": _clean((entry.get("认证材料") or {}).get("有效期")),
        }

    def update_materials(self, entry_id: int, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str, bool]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"认证记录 {entry_id} 不存在或已归档", False
        # 申请提交时材料与口径一起冻结，任何改写共享材料的请求都要被拒绝。
        return self.decorate(entry, None), (
            "认证材料为申请时冻结的共享材料（含标准核对表 STD-07、负责人签字与有效期），不能修改；"
            "如需调整请重新提交新的认证申请"
        ), False

    # ---------------------------------------------------------------- 有效期与可用集合
    def today_iso(self) -> str:
        return date.today().isoformat()

    def _parse_date(self, text: str, *, end_of_month: bool = False) -> date | None:
        match = _DATE_RE.search(text)
        if not match:
            return None
        year, month = int(match.group(1)), int(match.group(2))
        day_text = match.group(3)
        if day_text:
            day = int(day_text)
        elif end_of_month:
            day = calendar.monthrange(year, month)[1]
        else:
            day = 1
        try:
            return date(year, month, day)
        except ValueError:
            return None

    def _parse_validity(self, values: dict[str, Any]) -> tuple[date | None, date | None]:
        end_text = _clean(_first(values, VALIDITY_KEYS))
        start_text = _clean(_first(values, VALIDITY_START_KEYS))
        if "至" in end_text:
            left, right = end_text.split("至", 1)
            start_text = start_text or left
            end_text = right
        start_date = self._parse_date(start_text) if start_text else None
        end_date = self._parse_date(end_text, end_of_month=True) if end_text else None
        return start_date, end_date

    def _interval_text(self, start_date: date | None, end_date: date | None) -> str:
        if end_date is None:
            return ""
        if start_date is None:
            return end_date.isoformat()
        return f"{start_date.strftime('%Y-%m')} 至 {end_date.strftime('%Y-%m')}"

    def _mark_expired(self, entry: dict[str, Any], *, reason: str = "") -> None:
        entry["status"] = "已过期"
        entry["pending"] = False
        entry["abnormal"] = True
        entry["认证状态"] = "已过期"
        entry["认证结果"] = "已过期"
        entry["可用"] = False
        entry["available"] = False
        entry["发布准入"] = "未准入"
        if reason:
            entry["过期原因"] = reason
        entry["过期时间"] = self.today_iso()
        product = self._product_for_entry(entry)
        if product is not None:
            self._sync_product_admission(entry, product, admitted=False)

    def refresh_expiry(self) -> None:
        """把已过有效期但还没标记的认证立即置为过期，并撤下对应产品准入。

        申请中的认证过期时保留申请时的口径，只是不再占用“申请中”队列位置，
        这样重新提交同一标准时会得到明确提示而不是生成第二条审核结果。
        """
        today = date.today()
        for entry in store.rows(MODULE):
            if entry.get("status") not in ("已通过", "申请中"):
                continue
            end_text = _clean(entry.get("有效期至")) or _clean(entry.get("有效期限"))
            end_date = self._parse_date(end_text, end_of_month=True)
            if end_date is not None and end_date < today:
                self._mark_expired(entry, reason="有效期限已到")

    def is_available(self, entry: dict[str, Any]) -> bool:
        if entry.get("status") != "已通过":
            return False
        if _clean(entry.get("标准符合性结果")) != "通过":
            return False
        end_date = self._parse_date(_clean(entry.get("有效期至")) or _clean(entry.get("有效期限")), end_of_month=True)
        if end_date is not None and end_date < date.today():
            return False
        return bool(entry.get("available", True))

    def available_entries(self, product_no: str | None = None, actor: dict[str, str] | None = None) -> list[dict[str, Any]]:
        self.refresh_expiry()
        rows = [row for row in store.rows(MODULE) if self.is_available(row)]
        if product_no:
            rows = [row for row in rows if _clean(row.get("产品编号")) == product_no]
        return [self.decorate(row, actor) for row in rows]

    # ---------------------------------------------------------------- 产品发布准入
    def _admission_payload(self, entry: dict[str, Any]) -> dict[str, Any]:
        return {
            "认证记录id": entry.get("id"),
            "认证编号": _clean(entry.get("认证编号")),
            "产品编号": _clean(entry.get("产品编号")),
            "认证标准": _clean(entry.get("认证标准")),
            "标准符合性结果": _clean(entry.get("标准符合性结果")),
            "审核结论": _clean(entry.get("审核结论")),
            "有效期限": _clean(entry.get("有效期限")),
            "有效期至": _clean(entry.get("有效期至")),
            "认证有效": self.is_available(entry),
        }

    def _sync_product_admission(self, entry: dict[str, Any], product: dict[str, Any] | None, *, admitted: bool) -> None:
        if product is None:
            return
        if admitted:
            payload = self._admission_payload(entry)
            product["发布准入"] = "已准入"
            product["准入状态"] = "已准入"
            product["准入认证编号"] = payload["认证编号"]
            product["准入认证标准"] = payload["认证标准"]
            product["准入有效期至"] = payload["有效期至"]
            product["准入认证"] = payload
            product["准入更新时间"] = self.today_iso()
        else:
            product["发布准入"] = "未准入"
            product["准入状态"] = "未准入"
            product["准入认证编号"] = ""
            product["准入认证标准"] = ""
            product["准入有效期至"] = ""
            product["准入认证"] = None
            product["准入更新时间"] = self.today_iso()

    def admission_for_product(self, product: dict[str, Any] | None) -> dict[str, Any]:
        if product is None:
            return {"ok": True, "admitted": False, "准入状态": "未准入", "admission": None, "message": "产品不存在"}
        admission = product.get("准入认证")
        if isinstance(admission, dict):
            badge_id = admission.get("认证记录id")
            badge = store.find(MODULE, int(badge_id)) if badge_id is not None else None
            if badge is not None:
                self.refresh_expiry()
                if not self.is_available(badge):
                    self._sync_product_admission(badge, product, admitted=False)
                    admission = None
        if not admission:
            return {
                "ok": True,
                "admitted": False,
                "product_id": product.get("id"),
                "产品编号": _clean(product.get("产品编号")),
                "准入状态": "未准入",
                "admission": None,
                "certification": None,
                "message": "产品发布准入当前没有可用认证记录",
            }
        return {
            "ok": True,
            "admitted": True,
            "product_id": product.get("id"),
            "产品编号": _clean(product.get("产品编号")),
            "准入状态": _clean(product.get("准入状态")),
            "admission": admission,
            "certification": admission,
        }

    def admission_for_badge(self, entry_id: int) -> dict[str, Any] | None:
        self.refresh_expiry()
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None
        product = self._product_for_entry(entry)
        payload = self.admission_for_product(product)
        payload["认证编号"] = _clean(entry.get("认证编号"))
        payload["认证可用"] = self.is_available(entry)
        payload["credentials"] = self._admission_payload(entry) if payload.get("admitted") else None
        return payload
