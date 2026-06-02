from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from pathlib import Path
from typing import Any

from ..core.schemas import OrderLineInput, ProductRecord


def _normalize(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    stripped = stripped.replace("đ", "d").replace("Đ", "D")
    compact = re.sub(r"[^a-zA-Z0-9]+", " ", stripped.lower())
    return re.sub(r"\s+", " ", compact).strip()


class OrderDataStore:
    """
    Student TODO:
    - Load `products.json`.
    - Build lookup helpers for product IDs and normalized search.
    - Save final orders under `artifacts/orders/`.
    """

    def __init__(self, data_dir: Path, output_dir: Path, *, today: str | None = None) -> None:
        self.data_dir = Path(data_dir)
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.today = today or "2026-06-01"

        products_path = self.data_dir / "products.json"
        raw_products = json.loads(products_path.read_text(encoding="utf-8"))
        self.products = [ProductRecord(**item) for item in raw_products]
        self.product_index = {item.product_id: item for item in self.products}
        self.category_aliases = {
            "laptop": "laptop",
            "notebook": "laptop",
            "monitor": "monitor",
            "screen": "monitor",
            "man hinh": "monitor",
            "mouse": "mouse",
            "chuot": "mouse",
            "keyboard": "keyboard",
            "ban phim": "keyboard",
            "headphone": "headphone",
            "tai nghe": "headphone",
            "dock": "dock",
            "storage": "storage",
            "ssd": "storage",
            "stand": "stand",
            "webcam": "webcam",
        }

    @staticmethod
    def build_detail_token(product_ids: list[str]) -> str:
        normalized = "|".join(sorted(product_ids))
        return "DET-" + hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:10].upper()

    def validate_detail_token(self, product_ids: list[str], detail_token: str) -> bool:
        return detail_token == self.build_detail_token(product_ids)

    def canonicalize_category(self, value: str | None) -> str | None:
        if not value:
            return None
        return self.category_aliases.get(_normalize(value), _normalize(value))

    def list_products(
        self,
        *,
        query: str | None = None,
        category: str | None = None,
        max_unit_price: int | None = None,
        required_tags: list[str] | None = None,
        in_stock_only: bool = True,
        limit: int = 8,
    ) -> list[dict]:
        """
        Student TODO:
        - Search by product name, brand, category, tags, and description.
        - Return compact catalog summaries that the model can reuse in later tool calls.
        """
        normalized_query = _normalize(query or "")
        query_terms = [
            term
            for term in normalized_query.split()
            if term and not term.isdigit() and len(term) > 1
        ]
        wanted_category = self.canonicalize_category(category)
        wanted_tags = {_normalize(tag) for tag in (required_tags or []) if str(tag).strip()}
        results: list[tuple[int, int, str, dict[str, Any]]] = []

        for product in self.products:
            if in_stock_only and product.stock <= 0:
                continue
            if wanted_category and product.category != wanted_category:
                continue
            if max_unit_price is not None and product.unit_price > max_unit_price:
                continue

            haystack = _normalize(
                " ".join([product.name, product.brand, product.category, product.description, *product.tags])
            )
            score = 0
            matched_terms: list[str] = []
            for term in query_terms:
                if term in haystack:
                    score += 2
                    matched_terms.append(term)

            missing_tags: list[str] = []
            for tag in wanted_tags:
                if tag in haystack:
                    score += 3
                    matched_terms.append(tag)
                else:
                    missing_tags.append(tag)
            if missing_tags:
                continue

            if wanted_category:
                score += 3
            if query_terms and not matched_terms:
                continue

            results.append(
                (
                    score,
                    product.stock,
                    product.product_id,
                    {
                        "status": "ok",
                        "product_id": product.product_id,
                        "name": product.name,
                        "brand": product.brand,
                        "category": product.category,
                        "unit_price": product.unit_price,
                        "stock": product.stock,
                        "tags": product.tags,
                        "matched_terms": sorted(set(matched_terms)),
                        "next_step": "Call get_product_details with chosen product_id values before pricing or saving.",
                    },
                )
            )

        results.sort(key=lambda item: (-item[0], self.product_index[item[2]].unit_price, item[2]))
        matches = [item[-1] for item in results[:limit]]
        if matches:
            return matches
        return [
            {
                "status": "error",
                "message": "No catalog products matched the search filters. Do not invent product IDs; ask the user to choose another catalog item.",
                "errors": ["no_matching_products"],
                "query": query,
                "category": category,
                "required_tags": required_tags or [],
            }
        ]

    def get_product_details(self, product_ids: list[str]) -> dict:
        """
        Student TODO:
        - Return exact pricing, stock, category, and warranty information for each product ID.
        - Return a deterministic validation token that later tools can verify.
        - Preserve the input order or document how you reorder it.
        """
        requested_ids = [str(product_id).strip().upper() for product_id in product_ids if str(product_id).strip()]
        if not requested_ids:
            return self._error("No product IDs were provided.", "missing_product_ids")

        details: list[dict[str, Any]] = []
        found_product_ids: list[str] = []
        errors: list[str] = []
        for product_id in requested_ids:
            product = self.product_index.get(product_id)
            if not product:
                errors.append(f"Unknown product_id: {product_id}. Use product IDs returned by list_products.")
                details.append({"status": "error", "product_id": product_id, "message": "Product ID not found."})
                continue

            found_product_ids.append(product.product_id)
            details.append(
                {
                    "status": "ok",
                    "product_id": product.product_id,
                    "sku": product.sku,
                    "name": product.name,
                    "brand": product.brand,
                    "category": product.category,
                    "unit_price": product.unit_price,
                    "stock": product.stock,
                    "warranty_months": product.warranty_months,
                    "tags": product.tags,
                    "description": product.description,
                }
            )

        if errors:
            return {
                "status": "error",
                "message": "Some requested product IDs were not found. Stop and correct product selection before pricing.",
                "errors": errors,
                "detail_token": self.build_detail_token(found_product_ids) if found_product_ids else "",
                "items": details,
            }

        return {
            "status": "ok",
            "message": "Product details verified. Use detail_token in calculate_order_totals and save_order.",
            "detail_token": self.build_detail_token(found_product_ids),
            "items": details,
        }

    def get_discount(self, *, seed_hint: str, customer_tier: str = "standard") -> dict:
        """
        Student TODO:
        - Simulate a random campaign discount with deterministic seeding for grading.
        - Supported discount rates should be `0.1` or `0.2`.
        """
        normalized_seed = seed_hint.strip().lower()
        if not normalized_seed:
            return self._error("Missing seed_hint. Use the customer email as seed_hint, or phone as fallback.", "missing_seed_hint")

        normalized_tier = _normalize(customer_tier or "standard")
        if normalized_tier not in {"standard", "vip"}:
            return self._error("Unsupported customer_tier. Use 'standard' unless the user clearly states VIP.", "unsupported_customer_tier")

        digest = hashlib.sha256(f"{normalized_tier}|{normalized_seed}".encode("utf-8")).hexdigest()
        discount_rate = 0.2 if int(digest[-2:], 16) % 10 < 4 else 0.1
        return {
            "status": "ok",
            "message": "Campaign discount generated. Do not override this discount manually.",
            "seed_hint": seed_hint,
            "customer_tier": normalized_tier,
            "discount_rate": discount_rate,
            "campaign_code": f"FLASH-{int(discount_rate * 100):02d}",
        }

    def calculate_order_totals(self, *, items: list[OrderLineInput], detail_token: str, discount_rate: float) -> dict:
        """
        Student TODO:
        - Validate product IDs.
        - Validate the detail token produced by `get_product_details(...)`.
        - Validate requested quantities against stock.
        - Compute subtotal, discount amount, and final total.
        - Return an error payload instead of throwing for common user mistakes.
        """
        normalized_items = self._coerce_items(items)
        if not normalized_items:
            return self._error("No valid order items were provided.", "missing_items")

        if discount_rate not in {0.1, 0.2}:
            return self._error(
                f"Unsupported discount rate: {discount_rate}. Use only the rate returned by get_discount.",
                "unsupported_discount_rate",
            )

        requested_product_ids = [item.product_id for item in normalized_items]
        if not self.validate_detail_token(requested_product_ids, detail_token):
            return self._error(
                "Invalid detail token. Call get_product_details for the exact final product IDs before pricing.",
                "invalid_detail_token",
            )

        errors: list[str] = []
        lines: list[dict[str, Any]] = []
        subtotal = 0
        for item in sorted(normalized_items, key=lambda current: current.product_id):
            product = self.product_index.get(item.product_id)
            if not product:
                errors.append(f"Unknown product_id: {item.product_id}.")
                continue
            if item.quantity > product.stock:
                errors.append(
                    f"Insufficient stock for {product.name}: requested {item.quantity}, available {product.stock}."
                )
                continue

            line_total = product.unit_price * item.quantity
            subtotal += line_total
            lines.append(
                {
                    "product_id": product.product_id,
                    "sku": product.sku,
                    "name": product.name,
                    "category": product.category,
                    "quantity": item.quantity,
                    "unit_price": product.unit_price,
                    "line_total": line_total,
                }
            )

        if errors:
            return {
                "status": "error",
                "message": "Order cannot be priced or saved until these item problems are fixed.",
                "errors": errors,
                "items": lines,
            }

        discount_amount = int(subtotal * discount_rate)
        final_total = subtotal - discount_amount
        return {
            "status": "ok",
            "message": "Order totals calculated from verified catalog prices and stock.",
            "items": lines,
            "pricing": {
                "currency": "VND",
                "subtotal": subtotal,
                "discount_rate": discount_rate,
                "discount_amount": discount_amount,
                "final_total": final_total,
            },
            "detail_token": detail_token,
        }

    def save_order(
        self,
        *,
        customer_name: str,
        customer_phone: str,
        customer_email: str,
        shipping_address: str,
        items: list[OrderLineInput],
        detail_token: str,
        discount_rate: float,
        campaign_code: str,
        customer_tier: str = "standard",
        notes: str = "",
    ) -> dict:
        """
        Student TODO:
        - Recompute totals before saving.
        - Build a deterministic order ID.
        - Persist the final JSON payload to the output directory.
        - Return both the saved order payload and the saved file path.
        """
        customer_errors = self._validate_customer(
            customer_name=customer_name,
            customer_phone=customer_phone,
            customer_email=customer_email,
            shipping_address=shipping_address,
        )
        if customer_errors:
            return {
                "status": "error",
                "message": "Cannot save order because required customer fields are missing or invalid.",
                "errors": customer_errors,
            }

        normalized_items = self._coerce_items(items)
        pricing_snapshot = self.calculate_order_totals(
            items=normalized_items,
            detail_token=detail_token,
            discount_rate=discount_rate,
        )
        if pricing_snapshot["status"] != "ok":
            return {
                "status": "error",
                "message": "Cannot save order because pricing validation failed.",
                "errors": pricing_snapshot.get("errors", []),
                "pricing_result": pricing_snapshot,
            }

        expected_campaign_code = f"FLASH-{int(discount_rate * 100):02d}"
        if campaign_code != expected_campaign_code:
            return self._error(
                f"Campaign code {campaign_code!r} does not match discount_rate {discount_rate}. Expected {expected_campaign_code}.",
                "campaign_code_mismatch",
            )

        normalized_order_items = sorted(
            [{"product_id": item.product_id, "quantity": item.quantity} for item in normalized_items],
            key=lambda current: current["product_id"],
        )
        seed_payload = json.dumps(
            {
                "customer_email": customer_email.strip().lower(),
                "customer_phone": "".join(ch for ch in customer_phone if ch.isdigit()),
                "items": normalized_order_items,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        order_id = "ORD-" + hashlib.sha1(seed_payload.encode("utf-8")).hexdigest()[:10].upper()
        relative_path = Path("artifacts") / "orders" / f"{order_id}.json"
        absolute_path = self.output_dir / f"{order_id}.json"

        payload = {
            "order_id": order_id,
            "created_at": self.today,
            "status": "confirmed",
            "customer": {
                "name": customer_name.strip(),
                "phone": customer_phone.strip(),
                "email": customer_email.strip(),
                "shipping_address": shipping_address.strip(),
            },
            "items": pricing_snapshot["items"],
            "pricing": pricing_snapshot["pricing"],
            "discount": {
                "campaign_code": campaign_code,
                "customer_tier": customer_tier,
            },
            "save_path": str(relative_path),
            "source": "llm-order-agent",
        }
        if notes.strip():
            payload["notes"] = notes.strip()

        absolute_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        return {
            "status": "saved",
            "message": "Order saved successfully. Use saved_order.order_id, saved_order.pricing.final_total, and path in the final answer.",
            "order_id": order_id,
            "path": str(absolute_path),
            "saved_order": payload,
        }

    @staticmethod
    def _error(message: str, *errors: str) -> dict:
        return {"status": "error", "message": message, "errors": list(errors)}

    @staticmethod
    def _coerce_items(items: list[OrderLineInput] | list[dict] | Any) -> list[OrderLineInput]:
        normalized: list[OrderLineInput] = []
        if not isinstance(items, list):
            return normalized

        for item in items:
            if isinstance(item, OrderLineInput):
                normalized.append(item)
                continue
            if isinstance(item, dict):
                try:
                    normalized.append(OrderLineInput(**item))
                except Exception:
                    continue
        return normalized

    @staticmethod
    def _validate_customer(
        *,
        customer_name: str,
        customer_phone: str,
        customer_email: str,
        shipping_address: str,
    ) -> list[str]:
        errors: list[str] = []
        if not customer_name.strip():
            errors.append("Missing customer_name.")
        if not customer_phone.strip():
            errors.append("Missing customer_phone.")
        elif not re.fullmatch(r"[0-9 +().-]{8,20}", customer_phone.strip()):
            errors.append("Invalid customer_phone format.")
        if not customer_email.strip():
            errors.append("Missing customer_email.")
        elif not re.fullmatch(r"[\w.+-]+@[\w.-]+\.\w+", customer_email.strip()):
            errors.append("Invalid customer_email format.")
        if not shipping_address.strip():
            errors.append("Missing shipping_address.")
        return errors
