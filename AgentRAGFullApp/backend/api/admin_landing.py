"""Sprint 27 · Admin · Changelog + Testimonials CRUD.

Endpoints:
  GET    /v1/admin/changelog               · listar (incluye unpublished)
  POST   /v1/admin/changelog               · crear entry
  PATCH  /v1/admin/changelog/{slug}        · update
  DELETE /v1/admin/changelog/{slug}        · delete

  GET    /v1/admin/testimonials
  POST   /v1/admin/testimonials
  PATCH  /v1/admin/testimonials/{slug}
  DELETE /v1/admin/testimonials/{slug}
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from utils.admin_guard import (
    AdminPrincipal, require_saas_admin, require_saas_admin_role, audit_admin_action,
)

logger = logging.getLogger(__name__)
changelog_router = APIRouter(prefix="/v1/admin/changelog", tags=["admin-changelog"])
testimonials_router = APIRouter(prefix="/v1/admin/testimonials", tags=["admin-testimonials"])


# ══════════════════════════════════════════════════════════════════════
# CHANGELOG
# ══════════════════════════════════════════════════════════════════════


@changelog_router.get("")
async def list_changelog(admin: AdminPrincipal = Depends(require_saas_admin)):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch("select * from changelog_entries order by released_at desc")
    return {"items": [dict(r) for r in rows]}


class ChangelogCreate(BaseModel):
    slug: str = Field(pattern="^[a-z][a-z0-9-]+$", max_length=80)
    title: str = Field(min_length=3, max_length=200)
    summary: Optional[str] = None
    body_md: str = Field(min_length=10)
    category: str = Field(default="feature", pattern="^(feature|improvement|fix|breaking|announcement)$")
    version: Optional[str] = None
    highlighted: bool = False
    published: bool = True


@changelog_router.post("")
async def create_changelog(
    body: ChangelogCreate, request: Request,
    admin: AdminPrincipal = Depends(require_saas_admin_role("owner", "admin")),
):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        try:
            await conn.execute(
                """
                insert into changelog_entries (slug, title, summary, body_md, category,
                                                version, highlighted, published, created_by)
                values ($1, $2, $3, $4, $5, $6, $7, $8, $9::uuid)
                """,
                body.slug, body.title, body.summary, body.body_md, body.category,
                body.version, body.highlighted, body.published, admin.admin_user_id,
            )
        except Exception as e:
            if "unique" in str(e).lower():
                raise HTTPException(409, "Slug duplicado")
            raise
    await audit_admin_action(admin, "changelog.create", resource_type="changelog",
                             resource_id=body.slug, request=request, metadata=body.dict())
    return {"ok": True, "slug": body.slug}


class ChangelogPatch(BaseModel):
    title: Optional[str] = None
    summary: Optional[str] = None
    body_md: Optional[str] = None
    category: Optional[str] = None
    version: Optional[str] = None
    highlighted: Optional[bool] = None
    published: Optional[bool] = None


@changelog_router.patch("/{slug}")
async def update_changelog(
    slug: str, body: ChangelogPatch, request: Request,
    admin: AdminPrincipal = Depends(require_saas_admin_role("owner", "admin")),
):
    sets: list[str] = []
    params: list = [slug]
    for f, v in body.dict(exclude_none=True).items():
        params.append(v); sets.append(f"{f} = ${len(params)}")
    if not sets:
        raise HTTPException(400, "Nada que actualizar")
    sets.append("updated_at = now()")
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        await conn.execute(
            f"update changelog_entries set {', '.join(sets)} where slug = $1", *params,
        )
    await audit_admin_action(admin, "changelog.update", resource_type="changelog",
                             resource_id=slug, request=request,
                             metadata={"changes": body.dict(exclude_none=True)})
    return {"ok": True}


@changelog_router.delete("/{slug}")
async def delete_changelog(
    slug: str, request: Request,
    admin: AdminPrincipal = Depends(require_saas_admin_role("owner", "admin")),
):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        await conn.execute("delete from changelog_entries where slug = $1", slug)
    await audit_admin_action(admin, "changelog.delete", resource_type="changelog",
                             resource_id=slug, request=request)
    return {"ok": True}


# ══════════════════════════════════════════════════════════════════════
# TESTIMONIALS
# ══════════════════════════════════════════════════════════════════════


@testimonials_router.get("")
async def list_testimonials(admin: AdminPrincipal = Depends(require_saas_admin)):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch("select * from testimonials order by sort_order asc, created_at desc")
    return {"items": [dict(r) for r in rows]}


class TestimonialCreate(BaseModel):
    slug: str = Field(pattern="^[a-z][a-z0-9-]+$", max_length=80)
    author_name: str = Field(min_length=2, max_length=100)
    author_role: Optional[str] = None
    firm_name: Optional[str] = None
    firm_logo_url: Optional[str] = None
    avatar_url: Optional[str] = None
    quote: str = Field(min_length=10, max_length=1000)
    rating: Optional[int] = Field(default=None, ge=1, le=5)
    use_case: Optional[str] = None
    area_practica: Optional[str] = None
    country: str = "CO"
    featured: bool = False
    published: bool = True
    sort_order: int = 100


@testimonials_router.post("")
async def create_testimonial(
    body: TestimonialCreate, request: Request,
    admin: AdminPrincipal = Depends(require_saas_admin_role("owner", "admin")),
):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        try:
            await conn.execute(
                """
                insert into testimonials
                  (slug, author_name, author_role, firm_name, firm_logo_url, avatar_url,
                   quote, rating, use_case, area_practica, country, featured, published, sort_order)
                values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
                """,
                body.slug, body.author_name, body.author_role, body.firm_name,
                body.firm_logo_url, body.avatar_url, body.quote, body.rating,
                body.use_case, body.area_practica, body.country, body.featured,
                body.published, body.sort_order,
            )
        except Exception as e:
            if "unique" in str(e).lower():
                raise HTTPException(409, "Slug duplicado")
            raise
    await audit_admin_action(admin, "testimonials.create", resource_type="testimonial",
                             resource_id=body.slug, request=request)
    return {"ok": True, "slug": body.slug}


class TestimonialPatch(BaseModel):
    author_name: Optional[str] = None
    author_role: Optional[str] = None
    firm_name: Optional[str] = None
    avatar_url: Optional[str] = None
    quote: Optional[str] = None
    rating: Optional[int] = None
    area_practica: Optional[str] = None
    featured: Optional[bool] = None
    published: Optional[bool] = None
    sort_order: Optional[int] = None


@testimonials_router.patch("/{slug}")
async def update_testimonial(
    slug: str, body: TestimonialPatch, request: Request,
    admin: AdminPrincipal = Depends(require_saas_admin_role("owner", "admin")),
):
    sets: list[str] = []
    params: list = [slug]
    for f, v in body.dict(exclude_none=True).items():
        params.append(v); sets.append(f"{f} = ${len(params)}")
    if not sets:
        raise HTTPException(400, "Nada que actualizar")
    sets.append("updated_at = now()")
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        await conn.execute(
            f"update testimonials set {', '.join(sets)} where slug = $1", *params,
        )
    await audit_admin_action(admin, "testimonials.update", resource_type="testimonial",
                             resource_id=slug, request=request,
                             metadata={"changes": body.dict(exclude_none=True)})
    return {"ok": True}


@testimonials_router.delete("/{slug}")
async def delete_testimonial(
    slug: str, request: Request,
    admin: AdminPrincipal = Depends(require_saas_admin_role("owner", "admin")),
):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        await conn.execute("delete from testimonials where slug = $1", slug)
    await audit_admin_action(admin, "testimonials.delete", resource_type="testimonial",
                             resource_id=slug, request=request)
    return {"ok": True}
