from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel

T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int
    page_size: int
    pages: int


def make_page(items: list[T], total: int, page: int, page_size: int) -> Page[T]:
    return Page[T](items=items, total=total, page=page, page_size=page_size,
                   pages=max(1, -(-total // page_size)))
