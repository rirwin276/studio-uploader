"""Public display-only dashboard reads, batched into one Shopify query.

No owner IDs, customer tags, payout state, or private fundraiser totals leave
this endpoint. Existing single-store APIs remain available to other callers.
"""
import json
import re
from typing import Callable

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field


class DashboardRequest(BaseModel):
    handles: list[str] = Field(min_length=1, max_length=10)


def dashboard_router(graphql: Callable, shop_type: Callable, fundraiser_type: Callable):
    router = APIRouter()

    @router.post("/dashboard/state")
    def dashboard_state(body: DashboardRequest):
        handles = list(dict.fromkeys(body.handles))
        if any(not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,254}", h) for h in handles):
            return JSONResponse({"error": "Invalid store handle"}, status_code=400)
        declarations, selections, variables = [], [], {}
        for i, handle in enumerate(handles):
            for prefix, kind in (("s", shop_type()), ("f", fundraiser_type())):
                key = f"{prefix}{i}"
                declarations.append(f"${key}: MetaobjectHandleInput!")
                variables[key] = {"type": kind, "handle": handle}
                selections.append(f"{key}: metaobjectByHandle(handle: ${key}) {{ handle fields {{ key value }} }}")
        query = "query DashboardState(" + ", ".join(declarations) + ") {" + "\n".join(selections) + "}"
        try:
            data = graphql(query, variables)
        except Exception:
            return JSONResponse({"error": "Store status temporarily unavailable"}, status_code=502,
                                headers={"Cache-Control": "no-store"})
        stores = {}
        for i, handle in enumerate(handles):
            try:
                shop = data.get(f"s{i}")
                campaign = data.get(f"f{i}")
                if not shop or shop.get("handle") != handle:
                    stores[handle] = {"error": "Store unavailable"}
                    continue
                fields = {f["key"]: f.get("value") for f in shop.get("fields", [])}
                ready = str(fields.get("is_fully_ready") or "").lower() == "true"
                status = str(fields.get("status") or ("active" if ready else "building")).strip().lower()
                result = {"handle": handle, "ready": ready, "status": status}
                # Missing response keys indicate an incomplete upstream response,
                # not proof that a fundraiser is disabled.
                if f"f{i}" in data:
                    state = {}
                    if campaign:
                        if campaign.get("handle") != handle:
                            raise ValueError("Mismatched fundraiser")
                        raw = next((f.get("value") for f in campaign.get("fields", []) if f.get("key") == "data"), None)
                        state = json.loads(raw) if raw else {}
                        if not isinstance(state, dict):
                            raise ValueError("Invalid fundraiser")
                    public = {"handle": handle, "enabled": bool(state.get("enabled"))}
                    if public["enabled"]:
                        public["show_bar"] = bool(state.get("show_bar"))
                        if public["show_bar"]:
                            public.update(cause_name=state.get("cause_name") or "",
                                          goal=float(state.get("goal") or 0),
                                          total_raised=float(state.get("total_raised") or 0),
                                          end_date=state.get("end_date") or "")
                    result["fundraising"] = public
                stores[handle] = result
            except (ValueError, TypeError, KeyError, AttributeError):
                # One damaged record must not prevent other cards from loading.
                stores[handle] = {"error": "Store status temporarily unavailable"}
        return JSONResponse({"stores": stores}, headers={"Cache-Control": "no-store"})

    return router
