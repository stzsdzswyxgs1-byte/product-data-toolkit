"""
采集策略 - 店铺 / 搜索 / 详情 / 推荐
两步策略: 列表 API 批量抓取基础信息 -> 详情 API 补全图片与描述
"""

import json
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from curl_cffi import requests as curl_requests

from goofish_api import MtopClient, MtopError
from goofish_db import find_items, normalize_item, init_db, save_items
from goofish_rate import RateController
from goofish_session import IMPERSONATE


APIS = {
    "store": ("mtop.idle.web.xyh.item.list", "1.0"),
    "search": ("mtop.taobao.idlemtopsearch.pc.search", "1.0"),
    "feed": ("mtop.taobao.idlehome.home.webpc.feed", "1.0"),
    "detail": ("mtop.taobao.idle.pc.detail", "1.0"),
}


class GoofishCollector:
    def __init__(self, client: MtopClient, db_conn=None, on_log=None, on_progress=None,
                 min_price=0, max_price=0, publish_days=0, enrich_workers=4):
        self.client = client
        self.conn = db_conn or init_db()
        self.rate = RateController()
        self.seen_ids = set()
        self._stop = False
        self._on_log = on_log
        self._on_progress = on_progress
        self.min_price = float(min_price) if min_price else 0
        self.max_price = float(max_price) if max_price else 0
        self.publish_days = int(publish_days) if publish_days else 0
        self.enrich_workers = max(1, min(int(enrich_workers), 8))
        self._db_lock = threading.Lock()
        self._detail_logs = []

    def stop(self):
        self._stop = True

    def _log(self, msg):
        print(msg)
        if self._on_log:
            try:
                self._on_log(msg)
            except Exception:
                pass

    def _progress(self, current, total, info=""):
        if self._on_progress:
            try:
                self._on_progress(current, total, info)
            except Exception:
                pass

    def _price_ok(self, price_str):
        if not self.min_price and not self.max_price:
            return True
        try:
            p = float(price_str)
        except (ValueError, TypeError):
            return True
        if self.min_price and p < self.min_price:
            return False
        if self.max_price and p > self.max_price:
            return False
        return True

    def _time_ok(self, post_time):
        if not self.publish_days:
            return True
        if not post_time:
            return True
        try:
            ts = post_time
            if isinstance(ts, (int, float)) or (isinstance(ts, str) and ts.isdigit()):
                ts_int = int(ts)
                ts_sec = ts_int / 1000 if ts_int > 1e12 else ts_int
            else:
                return True
            age_days = (time.time() - ts_sec) / 86400
            return age_days <= self.publish_days
        except (ValueError, TypeError, OverflowError):
            return True

    def _is_auction(self, item_do):
        """檢測拍賣商品: itemType='detailAuction' 或 auctionDO 含 auctionId"""
        if not isinstance(item_do, dict):
            return False
        if str(item_do.get("itemType", "")) == "detailAuction":
            return True
        ad = item_do.get("auctionDO")
        if isinstance(ad, dict) and ad.get("auctionId"):
            return True
        return False

    def _is_sold(self, raw):
        status_str = str(raw.get("itemStatusStr", ""))
        if any(s in status_str for s in ("卖掉", "售出", "售罄", "下架", "删除")):
            return True

        for field in ("itemStatus", "status", "showStatus", "saleStatus", "sellStatus"):
            val = raw.get(field)
            if val is None:
                continue
            s = str(val).lower()
            if s in ("1", "2", "3", "sold", "soldout", "sold_out", "off_sale"):
                return True

        for field in ("soldOut", "isSoldOut", "isSold", "sold"):
            if raw.get(field) in (True, "true", "1", 1):
                return True

        for field in ("onSale", "onShelf", "available", "isOnSale"):
            if raw.get(field) in (False, "false", "0", 0):
                return True

        for field in ("stock", "availableQuantity", "remainQuantity"):
            val = raw.get(field)
            if val is None:
                continue
            try:
                if int(val) == 0:
                    return True
            except (ValueError, TypeError):
                pass

        # 方法6: 已售数量 + 无剩余库存 (组合判断)
        has_sold = False
        for field in ("soldQuantity", "soldNum", "soldCount", "soldCnt",
                       "tradeCount", "dealCount"):
            val = raw.get(field)
            if val is not None:
                try:
                    if int(val) > 0:
                        has_sold = True
                        break
                except (ValueError, TypeError):
                    pass
        if has_sold:
            for field in ("stock", "availableQuantity", "remainQuantity"):
                val = raw.get(field)
                if val is not None:
                    try:
                        if int(val) == 0:
                            return True
                    except (ValueError, TypeError):
                        pass

        # 方法7: 标签/tag字段包含已售文字
        for k, v in raw.items():
            kl = k.lower()
            if any(s in kl for s in ("tag", "badge", "mark", "label",
                                     "corner", "icon", "flag", "tip")):
                s = str(v) if isinstance(v, (str, int, float, bool)) \
                    else json.dumps(v, ensure_ascii=False)
                if "已售" in s or "售出" in s or "售罄" in s or "卖掉" in s:
                    return True

        return False

    def _upsert_item(self, item: dict, source: str = "detail"):
        item_id = str(item.get("itemId", "")).strip()
        if not item_id:
            return

        payload = (
            item_id,
            item.get("title", "") or "",
            str(item.get("price", "") or ""),
            json.dumps(item.get("images", []), ensure_ascii=False),
            item.get("description", "") or "",
            item.get("brief", "") or "",
            item.get("category", "") or "",
            item.get("location", "") or "",
            item.get("condition", "") or "",
            item.get("sellerId", "") or "",
            item.get("sellerNick", "") or "",
            item.get("sellerAvatar", "") or "",
            int(item.get("wantCount", 0) or 0),
            int(item.get("viewCount", 0) or 0),
            str(item.get("postTime", "") or ""),
            item.get("url", "") or "",
            datetime.now().isoformat(),
            source,
            item.get("originalPrice", "") or "",
            item.get("transportFee", "") or "",
            item.get("catDtoJson", "") or "",
            item.get("cpvLabelsJson", "") or "",
            int(item.get("soldCount", 0) or 0),
            item.get("itemStatus", "") or "",
            int(item.get("collectCount", 0) or 0),
            int(item.get("favorCount", 0) or 0),
            int(item.get("bargained", 0) or 0),
            item.get("itemTags", "") or "",
            item.get("promotionTag", "") or "",
            item.get("gmtCreate", "") or "",
            item.get("sellerUniqueName", "") or "",
            item.get("sellerSignature", "") or "",
            int(item.get("sellerSoldCount", 0) or 0),
            int(item.get("sellerItemCount", 0) or 0),
            int(item.get("sellerRegDays", 0) or 0),
            item.get("sellerGoodRate", "") or "",
            item.get("sellerReplyRate", "") or "",
            item.get("sellerReplyTime", "") or "",
            item.get("sellerLastActive", "") or "",
            int(item.get("sellerPlayboy", 0) or 0),
            int(item.get("sellerZhimaAuth", 0) or 0),
            item.get("sellerZhimaLevel", "") or "",
            int(item.get("sellerGoodRemark", 0) or 0),
            int(item.get("sellerBadRemark", 0) or 0),
            item.get("sellerCity", "") or "",
            item.get("groupName", "") or "",
            int(item.get("groupMemberCount", 0) or 0),
            item.get("categoryId2", "") or "",
            item.get("leafId", "") or "",
            item.get("itemLabelTexts", "") or "",
            item.get("soldPrice", "") or "",
            item.get("itemType", "") or "",
            item.get("gmtCreateStr", "") or "",
            item.get("commonTagsText", "") or "",
            item.get("videoUrl", "") or "",
            item.get("tradeAccessType", "") or "",
            item.get("sellerLevel", "") or "",
            item.get("sellerPortraitUrl", "") or "",
            item.get("sellerRegisterTime", "") or "",
            int(item.get("sellerYxpPro", 0) or 0),
            int(item.get("sellerDefaultRemark", 0) or 0),
            item.get("sellerIdentityTags", "") or "",
            item.get("sellerType", "") or "",
            item.get("labelPropsDetail", "") or "",
        )

        sql = """
            INSERT INTO products
            (item_id,title,price,images,description,brief,category,location,condition,
             seller_id,seller_nick,seller_avatar,want_count,view_count,post_time,url,collected_at,source,
             original_price,transport_fee,cat_dto_json,cpv_labels_json,sold_count,
             item_status,collect_count,favor_count,bargained,item_tags,promotion_tag,gmt_create,
             seller_unique_name,seller_signature,seller_sold_count,seller_item_count,seller_reg_days,
             seller_good_rate,seller_reply_rate,seller_reply_time,seller_last_active,
             seller_playboy,seller_zhima_auth,seller_zhima_level,seller_good_remark,seller_bad_remark,seller_city,
             group_name,group_member_count,
             category_id,leaf_id,item_label_texts,
             sold_price,item_type,gmt_create_str,common_tags_text,video_url,trade_access_type,
             seller_level,seller_portrait_url,seller_register_time,
             seller_yxp_pro,seller_default_remark,seller_identity_tags,seller_type,
             label_props_detail)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(item_id) DO UPDATE SET
                title=CASE WHEN excluded.title!='' THEN excluded.title ELSE products.title END,
                price=CASE WHEN excluded.price!='' THEN excluded.price ELSE products.price END,
                images=CASE WHEN excluded.images!='[]' AND excluded.images!='' THEN excluded.images ELSE products.images END,
                description=CASE WHEN excluded.description!='' THEN excluded.description ELSE products.description END,
                brief=CASE WHEN excluded.brief!='' THEN excluded.brief ELSE products.brief END,
                category=CASE WHEN excluded.category!='' THEN excluded.category ELSE products.category END,
                location=CASE WHEN excluded.location!='' THEN excluded.location ELSE products.location END,
                condition=CASE WHEN excluded.condition!='' THEN excluded.condition ELSE products.condition END,
                seller_id=CASE WHEN excluded.seller_id!='' THEN excluded.seller_id ELSE products.seller_id END,
                seller_nick=CASE WHEN excluded.seller_nick!='' THEN excluded.seller_nick ELSE products.seller_nick END,
                seller_avatar=CASE WHEN excluded.seller_avatar!='' THEN excluded.seller_avatar ELSE products.seller_avatar END,
                want_count=CASE WHEN excluded.want_count>0 THEN excluded.want_count ELSE products.want_count END,
                view_count=CASE WHEN excluded.view_count>0 THEN excluded.view_count ELSE products.view_count END,
                post_time=CASE WHEN excluded.post_time!='' THEN excluded.post_time ELSE products.post_time END,
                url=CASE WHEN excluded.url!='' THEN excluded.url ELSE products.url END,
                collected_at=excluded.collected_at,
                source=excluded.source,
                original_price=CASE WHEN excluded.original_price!='' THEN excluded.original_price ELSE products.original_price END,
                transport_fee=CASE WHEN excluded.transport_fee!='' THEN excluded.transport_fee ELSE products.transport_fee END,
                cat_dto_json=CASE WHEN excluded.cat_dto_json!='' THEN excluded.cat_dto_json ELSE products.cat_dto_json END,
                cpv_labels_json=CASE WHEN excluded.cpv_labels_json!='' THEN excluded.cpv_labels_json ELSE products.cpv_labels_json END,
                sold_count=CASE WHEN excluded.sold_count>0 THEN excluded.sold_count ELSE products.sold_count END,
                item_status=CASE WHEN excluded.item_status!='' THEN excluded.item_status ELSE products.item_status END,
                collect_count=CASE WHEN excluded.collect_count>0 THEN excluded.collect_count ELSE products.collect_count END,
                favor_count=CASE WHEN excluded.favor_count>0 THEN excluded.favor_count ELSE products.favor_count END,
                bargained=excluded.bargained,
                item_tags=CASE WHEN excluded.item_tags!='' THEN excluded.item_tags ELSE products.item_tags END,
                promotion_tag=CASE WHEN excluded.promotion_tag!='' THEN excluded.promotion_tag ELSE products.promotion_tag END,
                gmt_create=CASE WHEN excluded.gmt_create!='' THEN excluded.gmt_create ELSE products.gmt_create END,
                seller_unique_name=CASE WHEN excluded.seller_unique_name!='' THEN excluded.seller_unique_name ELSE products.seller_unique_name END,
                seller_signature=CASE WHEN excluded.seller_signature!='' THEN excluded.seller_signature ELSE products.seller_signature END,
                seller_sold_count=CASE WHEN excluded.seller_sold_count>0 THEN excluded.seller_sold_count ELSE products.seller_sold_count END,
                seller_item_count=CASE WHEN excluded.seller_item_count>0 THEN excluded.seller_item_count ELSE products.seller_item_count END,
                seller_reg_days=CASE WHEN excluded.seller_reg_days>0 THEN excluded.seller_reg_days ELSE products.seller_reg_days END,
                seller_good_rate=CASE WHEN excluded.seller_good_rate!='' THEN excluded.seller_good_rate ELSE products.seller_good_rate END,
                seller_reply_rate=CASE WHEN excluded.seller_reply_rate!='' THEN excluded.seller_reply_rate ELSE products.seller_reply_rate END,
                seller_reply_time=CASE WHEN excluded.seller_reply_time!='' THEN excluded.seller_reply_time ELSE products.seller_reply_time END,
                seller_last_active=CASE WHEN excluded.seller_last_active!='' THEN excluded.seller_last_active ELSE products.seller_last_active END,
                seller_playboy=excluded.seller_playboy,
                seller_zhima_auth=excluded.seller_zhima_auth,
                seller_zhima_level=CASE WHEN excluded.seller_zhima_level!='' THEN excluded.seller_zhima_level ELSE products.seller_zhima_level END,
                seller_good_remark=CASE WHEN excluded.seller_good_remark>0 THEN excluded.seller_good_remark ELSE products.seller_good_remark END,
                seller_bad_remark=CASE WHEN excluded.seller_bad_remark>0 THEN excluded.seller_bad_remark ELSE products.seller_bad_remark END,
                seller_city=CASE WHEN excluded.seller_city!='' THEN excluded.seller_city ELSE products.seller_city END,
                group_name=CASE WHEN excluded.group_name!='' THEN excluded.group_name ELSE products.group_name END,
                group_member_count=CASE WHEN excluded.group_member_count>0 THEN excluded.group_member_count ELSE products.group_member_count END,
                category_id=CASE WHEN excluded.category_id!='' THEN excluded.category_id ELSE products.category_id END,
                leaf_id=CASE WHEN excluded.leaf_id!='' THEN excluded.leaf_id ELSE products.leaf_id END,
                item_label_texts=CASE WHEN excluded.item_label_texts!='' THEN excluded.item_label_texts ELSE products.item_label_texts END,
                sold_price=CASE WHEN excluded.sold_price!='' THEN excluded.sold_price ELSE products.sold_price END,
                item_type=CASE WHEN excluded.item_type!='' THEN excluded.item_type ELSE products.item_type END,
                gmt_create_str=CASE WHEN excluded.gmt_create_str!='' THEN excluded.gmt_create_str ELSE products.gmt_create_str END,
                common_tags_text=CASE WHEN excluded.common_tags_text!='' THEN excluded.common_tags_text ELSE products.common_tags_text END,
                video_url=CASE WHEN excluded.video_url!='' THEN excluded.video_url ELSE products.video_url END,
                trade_access_type=CASE WHEN excluded.trade_access_type!='' THEN excluded.trade_access_type ELSE products.trade_access_type END,
                seller_level=CASE WHEN excluded.seller_level!='' THEN excluded.seller_level ELSE products.seller_level END,
                seller_portrait_url=CASE WHEN excluded.seller_portrait_url!='' THEN excluded.seller_portrait_url ELSE products.seller_portrait_url END,
                seller_register_time=CASE WHEN excluded.seller_register_time!='' THEN excluded.seller_register_time ELSE products.seller_register_time END,
                seller_yxp_pro=excluded.seller_yxp_pro,
                seller_default_remark=CASE WHEN excluded.seller_default_remark>0 THEN excluded.seller_default_remark ELSE products.seller_default_remark END,
                seller_identity_tags=CASE WHEN excluded.seller_identity_tags!='' THEN excluded.seller_identity_tags ELSE products.seller_identity_tags END,
                seller_type=CASE WHEN excluded.seller_type!='' THEN excluded.seller_type ELSE products.seller_type END,
                label_props_detail=CASE WHEN excluded.label_props_detail!='' THEN excluded.label_props_detail ELSE products.label_props_detail END
        """

        with self._db_lock:
            self.conn.execute(sql, payload)
            self.conn.commit()

    def _build_brief(self, item_do: dict) -> str:
        parts = []
        for cpv in item_do.get("cpvLabels", []):
            if isinstance(cpv, dict) and cpv.get("propertyName") and cpv.get("valueName"):
                parts.append(f"{cpv['propertyName']}：{cpv['valueName']}")
        return "\n".join(parts)

    def collect_store(self, user_id: str, max_pages: int = 999) -> int:
        self._stop = False
        api, ver = APIS["store"]
        collected = []
        self.seen_ids.clear()
        start = time.time()
        consecutive_empty = 0

        self._log(f"[店铺] 开始采集: {user_id}")

        for page in range(1, max_pages + 1):
            if self._stop:
                self._log("[店铺] 用户停止")
                break

            self.rate.wait_before(api)
            try:
                data = self.client.call(api, ver, {
                    "userId": str(user_id),
                    "pageNumber": str(page),
                    "pageSize": "20",
                })
            except MtopError as e:
                if e.code == "rate_limit":
                    self.rate.report_rate_limit(api)
                    continue
                if e.code == "unknown" and "FORBIDDEN" in str(e):
                    self._log("[店铺] 接口到达单店可见上限(通常约1000条在售)")
                self._log(f"[店铺] 第{page}页错误: {e}")
                break

            self.rate.report_success(api)

            items = find_items(data)
            if not items:
                self._log(f"[店铺] 第{page}页空数据, 采集完成")
                break

            page_new = 0
            skipped = 0
            skipped_sold = 0

            for raw in items:
                auction_type = raw.get("auctionType", "")
                raw_iid = str(raw.get("itemId", raw.get("id", "")))
                if auction_type and auction_type != "b":
                    skipped += 1
                    if raw_iid:
                        self._detail_logs.append(f"[拍卖过滤] {raw_iid}")
                    continue
                if self._is_sold(raw):
                    skipped += 1
                    skipped_sold += 1
                    if raw_iid:
                        self._detail_logs.append(f"[已售/下架] {raw_iid}")
                    continue

                item = normalize_item(raw)
                if not self._price_ok(item.get("price", "")):
                    skipped += 1
                    if raw_iid:
                        self._detail_logs.append(f"[价格过滤] {raw_iid}")
                    continue
                if not self._time_ok(item.get("postTime", "")):
                    skipped += 1
                    if raw_iid:
                        self._detail_logs.append(f"[时间过滤] {raw_iid}")
                    continue

                item_id = item.get("itemId", "")
                if item_id and item_id not in self.seen_ids:
                    self.seen_ids.add(item_id)
                    collected.append(item)
                    page_new += 1

            elapsed = time.time() - start
            speed = len(collected) / elapsed * 60 if elapsed > 0 else 0
            skip_note = f" 跳过{skipped}条" if skipped else ""
            self._log(f"  第{page}页 +{page_new}{skip_note} (共{len(collected)}, {speed:.0f}条/分)")
            self._progress(page, 0, f"{len(collected)}条 {speed:.0f}条/分")

            if skipped_sold == len(items):
                self._log(f"[店铺] 第{page}页全部已售, 在售商品采集完成")
                break

            if page_new == 0:
                consecutive_empty += 1
                if consecutive_empty >= 3:
                    self._log("[店铺] 连续无新数据, 采集完成")
                    break
            else:
                consecutive_empty = 0

        saved = save_items(self.conn, collected, f"store_{user_id}")
        elapsed = time.time() - start
        self._log(f"[店铺] 列表采集完成: {len(collected)}条, 新存{saved}条, 耗时{elapsed:.1f}s")

        if not self._stop:
            collected_ids = [item.get("itemId") for item in collected if item.get("itemId")]
            removed, enriched = self._enrich_and_filter(item_ids=collected_ids, max_workers=self.enrich_workers)
            if removed or enriched:
                final_count = self.conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
                self._log(f"[店铺] 最终结果: 补全{enriched}条, 移除{removed}条已售/下架 (库存{final_count}条)")

        self._progress(1, 1, "完成")
        return len(collected)

    def collect_search(self, keyword: str, max_pages: int = 999) -> int:
        self._stop = False
        api, ver = APIS["search"]
        collected = []
        self.seen_ids.clear()
        start = time.time()

        self._log(f"[搜索] 关键词: {keyword}")

        for page in range(1, max_pages + 1):
            if self._stop:
                self._log("[搜索] 用户停止")
                break

            self.rate.wait_before(api)
            try:
                data = self.client.call(api, ver, {
                    "keyword": keyword,
                    "pageNumber": str(page),
                    "pageSize": "20",
                })
            except MtopError as e:
                if e.code == "rate_limit":
                    self.rate.report_rate_limit(api)
                    continue
                self._log(f"[搜索] 第{page}页错误: {e}")
                break

            self.rate.report_success(api)
            items = find_items(data)
            if not items:
                self._log(f"[搜索] 第{page}页空数据, 完成")
                break

            page_new = 0
            skipped = 0
            for raw in items:
                auction_type = raw.get("auctionType", "")
                raw_iid = str(raw.get("itemId", raw.get("id", "")))
                if auction_type and auction_type != "b":
                    skipped += 1
                    if raw_iid:
                        self._detail_logs.append(f"[拍卖过滤] {raw_iid}")
                    continue
                if self._is_sold(raw):
                    skipped += 1
                    if raw_iid:
                        self._detail_logs.append(f"[已售/下架] {raw_iid}")
                    continue
                item = normalize_item(raw)
                if not self._price_ok(item.get("price", "")):
                    skipped += 1
                    if raw_iid:
                        self._detail_logs.append(f"[价格过滤] {raw_iid}")
                    continue
                if not self._time_ok(item.get("postTime", "")):
                    skipped += 1
                    if raw_iid:
                        self._detail_logs.append(f"[时间过滤] {raw_iid}")
                    continue
                item_id = item.get("itemId", "")
                if item_id and item_id not in self.seen_ids:
                    self.seen_ids.add(item_id)
                    collected.append(item)
                    page_new += 1

            elapsed = time.time() - start
            speed = len(collected) / elapsed * 60 if elapsed > 0 else 0
            skip_note = f" 跳过{skipped}条" if skipped else ""
            self._log(f"  第{page}页 +{page_new}{skip_note} (共{len(collected)}, {speed:.0f}条/分)")
            self._progress(page, 0, f"{len(collected)}条 {speed:.0f}条/分")

            if page_new == 0:
                break

        saved = save_items(self.conn, collected, f"search_{keyword}")
        elapsed = time.time() - start
        self._log(f"[搜索] 列表采集完成: {len(collected)}条, 新存{saved}条, 耗时{elapsed:.1f}s")

        if saved > 0 and not self._stop:
            collected_ids = [item.get("itemId") for item in collected if item.get("itemId")]
            removed, enriched = self._enrich_and_filter(item_ids=collected_ids, max_workers=self.enrich_workers)
            final_count = saved - removed
            self._log(f"[搜索] 最终结果: {final_count}条在售 (补全{enriched}, 移除{removed})")

        self._progress(1, 1, "完成")
        return len(collected)

    def collect_feed(self, max_pages: int = 999) -> int:
        self._stop = False
        api, ver = APIS["feed"]
        collected = []
        self.seen_ids.clear()
        start = time.time()

        self._log("[推荐] 首页推荐采集中...")

        for page in range(1, max_pages + 1):
            if self._stop:
                self._log("[推荐] 用户停止")
                break

            self.rate.wait_before(api)
            try:
                data = self.client.call(api, ver, {
                    "pageNumber": str(page),
                    "pageSize": "20",
                })
            except MtopError as e:
                if e.code == "rate_limit":
                    self.rate.report_rate_limit(api)
                    continue
                self._log(f"[推荐] 第{page}页错误: {e}")
                break

            self.rate.report_success(api)
            items = find_items(data)
            if not items:
                self._log(f"[推荐] 第{page}页空数据, 完成")
                break

            page_new = 0
            skipped = 0
            for raw in items:
                auction_type = raw.get("auctionType", "")
                raw_iid = str(raw.get("itemId", raw.get("id", "")))
                if auction_type and auction_type != "b":
                    skipped += 1
                    if raw_iid:
                        self._detail_logs.append(f"[拍卖过滤] {raw_iid}")
                    continue
                if self._is_sold(raw):
                    skipped += 1
                    if raw_iid:
                        self._detail_logs.append(f"[已售/下架] {raw_iid}")
                    continue
                item = normalize_item(raw)
                if not self._price_ok(item.get("price", "")):
                    skipped += 1
                    if raw_iid:
                        self._detail_logs.append(f"[价格过滤] {raw_iid}")
                    continue
                if not self._time_ok(item.get("postTime", "")):
                    skipped += 1
                    if raw_iid:
                        self._detail_logs.append(f"[时间过滤] {raw_iid}")
                    continue
                item_id = item.get("itemId", "")
                if item_id and item_id not in self.seen_ids:
                    self.seen_ids.add(item_id)
                    collected.append(item)
                    page_new += 1

            elapsed = time.time() - start
            speed = len(collected) / elapsed * 60 if elapsed > 0 else 0
            skip_note = f" 跳过{skipped}条" if skipped else ""
            self._log(f"  第{page}页 +{page_new}{skip_note} (共{len(collected)}, {speed:.0f}条/分)")
            self._progress(page, 0, f"{len(collected)}条 {speed:.0f}条/分")

            if page_new == 0:
                break

        saved = save_items(self.conn, collected, "feed")
        elapsed = time.time() - start
        self._log(f"[推荐] 列表采集完成: {len(collected)}条, 新存{saved}条, 耗时{elapsed:.1f}s")

        if saved > 0 and not self._stop:
            collected_ids = [item.get("itemId") for item in collected if item.get("itemId")]
            removed, enriched = self._enrich_and_filter(item_ids=collected_ids, max_workers=self.enrich_workers)
            final_count = saved - removed
            self._log(f"[推荐] 最终结果: {final_count}条在售 (补全{enriched}, 移除{removed})")

        self._progress(1, 1, "完成")
        return len(collected)

    def collect_detail(self, item_id: str, session=None, quiet=False) -> dict:
        api, ver = APIS["detail"]
        item_id = str(item_id).strip()
        if not item_id:
            return {}

        self.rate.wait_before("detail", fast=quiet)
        try:
            data = self.client.call(
                api,
                ver,
                {"itemId": item_id},
                referer=f"https://www.goofish.com/item?id={item_id}",
                session=session,
            )
            self.rate.report_success("detail")
        except MtopError as e:
            if e.code == "rate_limit":
                self.rate.report_rate_limit("detail")
            if not quiet:
                self._log(f"[详情] {item_id} 失败: {e}")
            return {}
        except Exception as e:
            if not quiet:
                self._log(f"[详情] {item_id} 异常: {type(e).__name__}: {e}")
            return {}

        item_do = data.get("itemDO", {})
        seller_do = data.get("sellerDO", {})

        if not item_do:
            # fallback: 尝试用 find_items 解析
            fallback_items = find_items(data)
            if fallback_items:
                item = normalize_item(fallback_items[0])
                item["itemId"] = item_id
                if not self._price_ok(item.get("price", "")):
                    return {"itemId": item_id, "__filtered": "price"}
                if not self._time_ok(item.get("postTime", "")):
                    return {"itemId": item_id, "__filtered": "time"}
                self._upsert_item(item, source="detail")
                return item
            if not quiet:
                self._log(f"[详情] {item_id} 无 itemDO 且 fallback 失败")
            return {}

        raw_status = item_do.get("itemStatus")
        try:
            item_status = int(raw_status) if raw_status is not None else 0
        except (ValueError, TypeError):
            item_status = 0
        status_str = str(item_do.get("itemStatusStr", ""))
        is_sold = item_status != 0 or any(k in status_str for k in ("卖掉", "售出", "下架", "删除"))
        if is_sold:
            if not quiet:
                self._log(f"[详情] {item_id} 已售/下架")
            return {"itemId": item_id, "__sold": True}

        if self._is_auction(item_do):
            if not quiet:
                self._log(f"[详情] {item_id} 拍卖商品, 跳过")
            return {"itemId": item_id, "__filtered": "auction"}

        # 從 itemDO + sellerDO 提取完整字段
        # 順序: 視頻封面 (若有) → imageInfos 各圖 → (fallback) defaultPicture
        # 用 seen set 去重 (視頻封面常和 imageInfos[0] 相同 URL)
        images = []
        seen = set()
        def _add(u):
            if isinstance(u, str) and u.startswith(("http://", "https://", "//")) and u not in seen:
                images.append(u)
                seen.add(u)
        # 1) 視頻商品: 視頻封面作為第一張圖
        vpi = item_do.get("videoPlayInfo", {})
        if isinstance(vpi, dict):
            _add(vpi.get("url", ""))
        # 2) 接上 imageInfos 所有圖
        for img in item_do.get("imageInfos", []):
            if isinstance(img, dict):
                _add(img.get("url", ""))
        # 3) 都沒有 → fallback 到 defaultPicture (必須是 http URL, 不能是 "false" 字串)
        if not images:
            dp = item_do.get("defaultPicture")
            if isinstance(dp, str) and dp.startswith(("http://", "https://", "//")):
                images.append(dp)
            # 最後保底: 從 imageInfos[0].photoSearchUrl 解碼 raw URL
            for img in item_do.get("imageInfos", []):
                if not isinstance(img, dict): continue
                psu = img.get("photoSearchUrl", "")
                if psu and "url%22%3A%22" in psu:
                    import urllib.parse as _p
                    try:
                        decoded = _p.unquote(_p.unquote(psu))
                        i = decoded.find('"url":"')
                        if i > 0:
                            i += 7
                            j = decoded.find('"', i)
                            if j > 0:
                                images.append(decoded[i:j])
                    except Exception: pass

        item = normalize_item(item_do)
        item["itemId"] = item_id
        if images:
            item["images"] = images
        item["description"] = item_do.get("desc", "") or " "
        item["brief"] = self._build_brief(item_do)

        if item_do.get("title"):
            item["title"] = item_do["title"]
        if item_do.get("gmtCreate"):
            item["postTime"] = item_do.get("gmtCreate")
        item["price"] = str(item_do.get("soldPrice", item_do.get("defaultPrice", ""))) or item.get("price", "")
        # 分类: catId|channelCatId|细分名称|tbCatId
        cat_name = ""
        channel_cat_id = ""
        for lbl in item_do.get("itemLabelExtList", []):
            props = lbl.get("properties", "")
            if "分类:" in props and "##" in props:
                parts = props.split("##")
                if len(parts) >= 3:
                    cat_name = parts[-1]
                channel_cat_id = str(lbl.get("channelCateId", ""))
                break
        cat_dto = item_do.get("itemCatDTO", {})
        cat_id = str(cat_dto.get("catId", "") or item_do.get("categoryId", ""))
        tb_cat_id = str(cat_dto.get("tbCatId", "") or "")
        if not channel_cat_id:
            channel_cat_id = str(cat_dto.get("channelCatId", ""))
        item["category"] = f"{cat_id}|{channel_cat_id}|{cat_name}|{tb_cat_id}"
        item["condition"] = item_do.get("stuffStatus", "") or item.get("condition", "")

        # ── 扩展字段: 完整分类DTO + 属性标签 + 原价/运费/已售 ──
        import json as _json
        item["catDtoJson"] = _json.dumps(cat_dto, ensure_ascii=False) if cat_dto else ""
        cpv_labels = item_do.get("cpvLabels", [])
        item["cpvLabelsJson"] = _json.dumps(cpv_labels, ensure_ascii=False) if cpv_labels else ""
        item["originalPrice"] = str(item_do.get("originalPrice", "") or "")
        item["transportFee"] = str(item_do.get("transportFee", "") or "")
        item["soldCount"] = int(item_do.get("soldCnt", 0) or 0)

        # ── 商品扩展 ──
        item["itemStatus"] = item_do.get("itemStatusStr", "") or ""
        item["collectCount"] = int(item_do.get("collectCnt", 0) or 0)
        item["favorCount"] = int(item_do.get("favorCnt", 0) or 0)
        item["bargained"] = 1 if item_do.get("bargained") else 0
        # 标签(包邮/保障等)
        tags = []
        for ct in item_do.get("commonTags", []):
            t = ct.get("text", "")
            if t:
                tags.append(t)
        item["itemTags"] = _json.dumps(tags, ensure_ascii=False) if tags else ""
        # 促销标签
        promo = item_do.get("promotionPriceDO", {})
        item["promotionTag"] = promo.get("promotionPriceTag", "") or ""
        item["gmtCreate"] = str(item_do.get("gmtCreate", "") or "")

        # ── 卖家扩展 ──
        if seller_do:
            item["sellerUniqueName"] = seller_do.get("uniqueName", "") or ""
            item["sellerSignature"] = seller_do.get("signature", "") or ""
            item["sellerSoldCount"] = int(seller_do.get("hasSoldNumInteger", 0) or 0)
            item["sellerItemCount"] = int(seller_do.get("itemCount", 0) or 0)
            item["sellerRegDays"] = int(seller_do.get("userRegDay", 0) or 0)
            item["sellerGoodRate"] = seller_do.get("newGoodRatioRate", "") or ""
            item["sellerReplyRate"] = seller_do.get("replyRatio24h", "") or ""
            item["sellerReplyTime"] = seller_do.get("replyInterval", "") or ""
            item["sellerLastActive"] = seller_do.get("lastVisitTime", "") or ""
            item["sellerPlayboy"] = 1 if seller_do.get("playboy") else 0
            item["sellerZhimaAuth"] = 1 if seller_do.get("zhimaAuth") else 0
            zhima = seller_do.get("zhimaLevelInfo", {})
            item["sellerZhimaLevel"] = zhima.get("levelName", "") if isinstance(zhima, dict) else ""
            remark = seller_do.get("remarkDO", {})
            if isinstance(remark, dict):
                item["sellerGoodRemark"] = int(remark.get("sellerGoodRemarkCnt", 0) or 0)
                item["sellerBadRemark"] = int(remark.get("sellerBadRemarkCnt", 0) or 0)
            item["sellerCity"] = seller_do.get("city", seller_do.get("publishCity", "")) or ""

        # ── 圈子 ──
        group = data.get("groupDO", {})
        if isinstance(group, dict) and group.get("name"):
            item["groupName"] = group.get("name", "") or ""
            gc = group.get("userCnt", "")
            if isinstance(gc, str):
                gc = int(gc.replace("个", "").replace(",", "").strip() or 0)
            item["groupMemberCount"] = int(gc or 0)
        item["wantCount"] = int(item_do.get("wantCnt", 0) or 0) or item.get("wantCount", 0)
        item["viewCount"] = int(item_do.get("browseCnt", 0) or 0) or item.get("viewCount", 0)

        # ── 第二批深挖: 分类/商品/卖家扩展 ──
        # 分类深挖
        item["categoryId2"] = str(item_do.get("categoryId", "") or "")
        item["leafId"] = str(cat_dto.get("leafId", "") or "")
        # 分类标签文字列表 (如 "非流通外国钱币 | 古代 | 欧洲 | 银 | 上品")
        label_texts = []
        label_props_detail = []  # 完整属性解析
        for lbl in item_do.get("itemLabelExtList", []):
            t = lbl.get("text", "")
            if t:
                label_texts.append(t)
            # 解析 properties: "propertyId##propertyName:valueId##valueName"
            props = lbl.get("properties", "")
            if props and "##" in props:
                pp = props.split("##")
                if len(pp) >= 3:
                    prop_name_val = pp[1]  # "propertyName:valueId" or "分类:126862234"
                    val_name = pp[2]       # valueName
                    label_props_detail.append(f"{prop_name_val.split(':')[0]}={val_name}")
        item["itemLabelTexts"] = " | ".join(label_texts) if label_texts else ""
        item["labelPropsDetail"] = " | ".join(label_props_detail) if label_props_detail else ""

        # 商品深挖
        item["soldPrice"] = str(item_do.get("soldPrice", "") or "")
        item["itemType"] = item_do.get("itemType", "") or ""
        item["gmtCreateStr"] = item_do.get("GMT_CREATE_DATE_KEY", "") or ""
        # 通用标签 (已在 itemTags 里存了 commonTags, 这里再存一份纯文字)
        common_tag_texts = []
        for ct in item_do.get("commonTags", []):
            t = ct.get("text", "")
            if t:
                common_tag_texts.append(t)
        item["commonTagsText"] = " | ".join(common_tag_texts) if common_tag_texts else ""
        # 视频
        vi = item_do.get("videoPlayInfo", {})
        if isinstance(vi, dict):
            item["videoUrl"] = vi.get("playUrl") or vi.get("url", "") or ""
        item["tradeAccessType"] = str(item_do.get("tradeAccessType", "") or "")

        # 卖家深挖
        if seller_do:
            # 卖家等级
            for lt in seller_do.get("levelTags", []):
                tp = lt.get("trackParams", {})
                if isinstance(tp, dict) and tp.get("sellerLevel"):
                    item["sellerLevel"] = str(tp["sellerLevel"])
                    break
            item["sellerPortraitUrl"] = seller_do.get("portraitUrl", "") or ""
            item["sellerRegisterTime"] = str(seller_do.get("registerTime", "") or "")
            item["sellerYxpPro"] = 1 if seller_do.get("yxpPro") else 0
            remark = seller_do.get("remarkDO", {})
            if isinstance(remark, dict):
                item["sellerDefaultRemark"] = int(remark.get("sellerDefaultRemarkCnt", 0) or 0)
            # 身份认证标签
            id_tags = []
            for it in seller_do.get("identityTags", []):
                t = it.get("text", "")
                if t:
                    id_tags.append(t)
            item["sellerIdentityTags"] = " | ".join(id_tags) if id_tags else ""
            item["sellerType"] = seller_do.get("sellerTypeString", "") or ""

        # sellerDO 字段
        if seller_do:
            item["sellerId"] = str(seller_do.get("sellerId", "")) or item.get("sellerId", "")
            item["sellerNick"] = seller_do.get("nick", "") or item.get("sellerNick", "")
            item["sellerAvatar"] = seller_do.get("avatar", seller_do.get("avatarUrl", "")) or item.get("sellerAvatar", "")
            item["location"] = seller_do.get("city", seller_do.get("publishCity", "")) or item.get("location", "")

        # 价格/时间过滤（商品模式 & 云端详情也生效）
        if not self._price_ok(item.get("price", "")):
            if not quiet:
                self._log(f"[详情] {item_id} 价格不符过滤条件, 跳过")
            return {"itemId": item_id, "__filtered": "price"}
        if not self._time_ok(item.get("postTime", "")):
            if not quiet:
                self._log(f"[详情] {item_id} 发布时间不符过滤条件, 跳过")
            return {"itemId": item_id, "__filtered": "time"}

        self._upsert_item(item, source="detail")

        if not quiet:
            self._log(f"[详情] {item_id} 完成: 图片{len(item.get('images', []))}张")
        return item

    def collect_details_concurrent(self, item_ids, max_workers=5):
        ids = [str(x).strip() for x in item_ids if str(x).strip()]
        if not ids:
            self._log("[并发详情] 没有可采集ID")
            return [], 0, 0

        # 保序去重
        seen = set()
        uniq = []
        for iid in ids:
            if iid not in seen:
                seen.add(iid)
                uniq.append(iid)

        total = len(uniq)
        try:
            max_workers = int(max_workers)
        except (ValueError, TypeError):
            max_workers = 5
        max_workers = max(1, min(max_workers, 16))

        self._log(f"[并发详情] 开始采集 {total} 个商品 ({max_workers}线程)...")

        # 为每个 worker 创建独立 session，复制主 session 的 cookie（认证凭据）
        def _make_session():
            s = curl_requests.Session(impersonate=IMPERSONATE)
            main_s = self.client.gs.get_session()
            for c in main_s.cookies.jar:
                s.cookies.set(c.name, c.value, domain=c.domain, path=c.path)
            return s

        sessions = [_make_session() for _ in range(max_workers)]
        done_ids = []
        skipped = 0
        completed = 0
        start = time.time()
        lock = threading.Lock()

        def _work(one_id, worker_session):
            result = self.collect_detail(one_id, session=worker_session, quiet=True)
            return one_id, result

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = [pool.submit(_work, iid, sessions[i % max_workers]) for i, iid in enumerate(uniq)]
            for future in as_completed(futures):
                if self._stop:
                    pool.shutdown(wait=False, cancel_futures=True)
                    break

                item_id, result = future.result()
                with lock:
                    completed += 1
                    if result and not result.get("__sold") and not result.get("__filtered"):
                        done_ids.append(item_id)
                        self._detail_logs.append(f"[保存] {item_id}")
                    else:
                        skipped += 1
                        if result and result.get("__sold"):
                            self._detail_logs.append(f"[已售/下架] {item_id}")
                        elif result and result.get("__filtered") == "price":
                            self._detail_logs.append(f"[价格过滤] {item_id}")
                        elif result and result.get("__filtered") == "time":
                            self._detail_logs.append(f"[时间过滤] {item_id}")
                        elif result and result.get("__filtered") == "auction":
                            self._detail_logs.append(f"[拍卖过滤] {item_id}")
                        elif result and result.get("__filtered"):
                            self._detail_logs.append(f"[价格过滤] {item_id}")
                        else:
                            self._detail_logs.append(f"[错误] {item_id}")

                    if completed % 20 == 0 or completed == total:
                        elapsed = time.time() - start
                        speed = completed / elapsed * 60 if elapsed > 0 else 0
                        self._progress(completed, total, f"并发采集 {completed}/{total} ({speed:.0f}条/分)")
                    if completed % 100 == 0 or completed == total:
                        self._log(f"  [并发详情] 进度 {completed}/{total}, 成功{len(done_ids)}, 跳过{skipped}")

        self._log(f"[并发详情] 完成: {total}个商品, 保存{len(done_ids)}个, 跳过{skipped}个")
        return done_ids, len(done_ids), skipped

    def enrich_missing(self, limit: int = 100) -> int:
        self._stop = False
        rows = self.conn.execute(
            "SELECT item_id FROM products WHERE "
            "images IN ('[]', '[\"\"]', '') OR length(images) < 10 "
            "OR description = '' OR description IS NULL "
            "ORDER BY CAST(item_id AS INTEGER) DESC LIMIT ?",
            (limit,),
        ).fetchall()

        if not rows:
            self._log("[补全] 无需补全")
            return 0

        self._log(f"[补全] 需补全 {len(rows)} 条")
        enriched = 0
        for i, (item_id,) in enumerate(rows):
            if self._stop:
                self._log("[补全] 用户停止")
                break
            try:
                item = self.collect_detail(str(item_id), quiet=True)
                if item and not item.get("__sold") and not item.get("__filtered"):
                    enriched += 1
            except Exception as e:
                self._log(f"[补全] {item_id} 失败: {e}")
            if (i + 1) % 20 == 0 or (i + 1) == len(rows):
                self._progress(i + 1, len(rows), f"补全{enriched}条")

        self._log(f"[补全] 完成: {enriched}/{len(rows)}")
        self._progress(len(rows), len(rows), "完成")
        return enriched

    def batch_stores(self, user_ids: list, max_pages: int = 999) -> int:
        self._stop = False
        total = 0
        for i, uid in enumerate(user_ids):
            if self._stop:
                self._log("[批量] 用户停止")
                break
            self._log(f"[批量] 进度: {i + 1}/{len(user_ids)}")
            total += self.collect_store(str(uid), max_pages=max_pages)
            if i < len(user_ids) - 1 and not self._stop:
                wait = random.uniform(3.0, 8.0)
                self._log(f"  店间等待 {wait:.1f}s...")
                time.sleep(wait)
        return total

    def _enrich_and_filter(self, item_ids=None, max_workers=1):
        if item_ids:
            placeholders = ','.join('?' * len(item_ids))
            rows = self.conn.execute(
                f"SELECT DISTINCT item_id FROM products WHERE "
                f"(description = '' OR description IS NULL OR images = '[]' OR images = '') "
                f"AND item_id IN ({placeholders}) "
                f"ORDER BY CAST(item_id AS INTEGER) DESC",
                list(item_ids),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT DISTINCT item_id FROM products WHERE "
                "description = '' OR description IS NULL OR images = '[]' OR images = '' "
                "ORDER BY CAST(item_id AS INTEGER) DESC"
            ).fetchall()

        if not rows:
            self._log("[验证] 所有商品已验证且信息完整")
            return 0, 0

        total = len(rows)
        max_workers = max(1, min(int(max_workers), 8))
        self._log(f"[验证] {total} 个商品需验证 (按新到旧, {max_workers}线程)...")

        api, ver = APIS["detail"]
        start = time.time()

        # ── 阶段1: 并发调 API, 收集原始响应 ──
        all_ids = [str(r[0]) for r in rows]
        results = {}  # item_id -> data dict (或 None 表示失败)
        rate_limited = []  # 限流失败的 id, 后面单线程重试

        if max_workers > 1:
            def _make_session():
                s = curl_requests.Session(impersonate=IMPERSONATE)
                main_s = self.client.gs.get_session()
                for c in main_s.cookies.jar:
                    s.cookies.set(c.name, c.value, domain=c.domain, path=c.path)
                return s

            sessions = [_make_session() for _ in range(max_workers)]

            def _fetch(item_id, sess):
                if self._stop:
                    return item_id, None, "stop"
                self.rate.wait_before("detail", fast=True)
                try:
                    data = self.client.call(api, ver, {"itemId": item_id},
                                            referer=f"https://www.goofish.com/item?id={item_id}",
                                            session=sess)
                    self.rate.report_success("detail")
                    return item_id, data, None
                except MtopError as e:
                    if e.code == "rate_limit":
                        self.rate.report_rate_limit("detail")
                        return item_id, None, "rate_limit"
                    return item_id, None, "error"
                except Exception:
                    return item_id, None, "error"

            fetched = 0
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                futures = [pool.submit(_fetch, iid, sessions[i % max_workers])
                           for i, iid in enumerate(all_ids)]
                for future in as_completed(futures):
                    if self._stop:
                        pool.shutdown(wait=False, cancel_futures=True)
                        break
                    item_id, data, err = future.result()
                    if err == "rate_limit":
                        rate_limited.append(item_id)
                    elif err is None:
                        results[item_id] = data
                    # else: error, 留空 → 后面算 skipped
                    fetched += 1
                    if fetched % 100 == 0 or fetched == total:
                        elapsed = time.time() - start
                        speed = fetched / elapsed * 60 if elapsed > 0 else 0
                        self._progress(fetched, total * 2, f"API请求 {fetched}/{total} ({speed:.0f}条/分)")

            # 限流失败的单线程重试 (带退避)
            for item_id in rate_limited:
                if self._stop:
                    break
                time.sleep(random.uniform(2.0, 4.0))
                try:
                    data = self.client.call(api, ver, {"itemId": item_id},
                                            referer=f"https://www.goofish.com/item?id={item_id}")
                    self.rate.report_success("detail")
                    results[item_id] = data
                except Exception:
                    pass  # 重试也失败 → skipped

            phase1_elapsed = time.time() - start
            self._log(f"[验证] API请求完成: {len(results)}/{total}条成功, "
                      f"耗时{phase1_elapsed:.1f}s ({len(results)/phase1_elapsed*60:.0f}条/分)")
        else:
            # 单线程: 逐条请求 (兼容旧逻辑)
            for i, item_id in enumerate(all_ids):
                if self._stop:
                    break
                self.rate.wait_before("detail", fast=True)
                try:
                    data = self.client.call(api, ver, {"itemId": item_id},
                                            referer=f"https://www.goofish.com/item?id={item_id}")
                    self.rate.report_success("detail")
                    results[item_id] = data
                except MtopError as e:
                    if e.code == "rate_limit":
                        self.rate.report_rate_limit("detail")
                        self._log(f"  [!] 触发限流, 退避中...")
                        time.sleep(random.uniform(3.0, 5.0))
                        # 重试
                        try:
                            data = self.client.call(api, ver, {"itemId": item_id},
                                                    referer=f"https://www.goofish.com/item?id={item_id}")
                            results[item_id] = data
                        except Exception:
                            pass
                except Exception:
                    pass
                if (i + 1) % 100 == 0:
                    elapsed = time.time() - start
                    speed = (i + 1) / elapsed * 60 if elapsed > 0 else 0
                    self._progress(i + 1, total * 2, f"API请求 {i+1}/{total} ({speed:.0f}条/分)")

        # ── 阶段2: 按原顺序处理结果 (DB操作 + 连续过期判断) ──
        removed = 0
        removed_sold = 0
        removed_filter = 0
        enriched = 0
        skipped = 0
        consecutive_expired = 0

        for i, item_id in enumerate(all_ids):
            if self._stop:
                self._log("[验证] 用户停止")
                break

            data = results.get(item_id)
            if data is None:
                skipped += 1
                self._detail_logs.append(f"[错误] {item_id}")
                completed = i + 1
                if completed % 100 == 0 or completed == total:
                    self._progress(total + completed, total * 2, f"处理 {completed}/{total}")
                continue

            item_do = data.get("itemDO", {})

            if not item_do:
                raw_text = json.dumps(data, ensure_ascii=False)
                if any(k in raw_text for k in ("宝贝不存在", "已删除", "商品已下架")):
                    self.conn.execute("DELETE FROM products WHERE item_id=?", (item_id,))
                    self.conn.commit()
                    removed += 1
                    removed_sold += 1
                    self._detail_logs.append(f"[已售/下架] {item_id}")
                else:
                    skipped += 1
                    self._detail_logs.append(f"[错误] {item_id}")
                completed = i + 1
                if completed % 100 == 0 or completed == total:
                    self._progress(total + completed, total * 2, f"处理 {completed}/{total}")
                continue

            # 检查在售状态
            raw_status = item_do.get("itemStatus")
            try:
                item_status = int(raw_status) if raw_status is not None else 0
            except (ValueError, TypeError):
                item_status = 0
            status_str = str(item_do.get("itemStatusStr", ""))
            is_sold = item_status != 0 or any(k in status_str for k in ("卖掉", "下架", "删除", "售出"))

            if is_sold:
                self.conn.execute("DELETE FROM products WHERE item_id=?", (item_id,))
                self.conn.commit()
                removed += 1
                removed_sold += 1
                self._detail_logs.append(f"[已售/下架] {item_id}")
                consecutive_expired = 0
            elif self._is_auction(item_do):
                self.conn.execute("DELETE FROM products WHERE item_id=?", (item_id,))
                self.conn.commit()
                removed += 1
                removed_filter += 1
                self._detail_logs.append(f"[拍卖过滤] {item_id}")
                consecutive_expired = 0
            else:
                # 价格/时间二次验证 (detail API 价格更准确)
                detail_price = str(item_do.get("soldPrice", item_do.get("defaultPrice", "")))
                detail_time = item_do.get("gmtCreate", item_do.get("GMT_CREATE_DATE_KEY", ""))
                price_ok = self._price_ok(detail_price)
                time_ok = self._time_ok(detail_time)

                if not price_ok or not time_ok:
                    self.conn.execute("DELETE FROM products WHERE item_id=?", (item_id,))
                    self.conn.commit()
                    removed += 1
                    removed_filter += 1
                    if not time_ok:
                        self._detail_logs.append(f"[时间过滤] {item_id}")
                    else:
                        self._detail_logs.append(f"[价格过滤] {item_id}")
                    if self.publish_days and not time_ok:
                        consecutive_expired += 1
                    else:
                        consecutive_expired = 0
                else:
                    consecutive_expired = 0

                    # 在售 → 完整補全: 圖片 (含視頻封面) + 描述 + 標題 + 分類 + seller
                    # 順序: 視頻封面 → imageInfos → defaultPicture fallback (去重)
                    images = []
                    seen_u = set()
                    def _add_img(u):
                        if isinstance(u, str) and u.startswith(("http://","https://","//")) and u not in seen_u:
                            images.append(u); seen_u.add(u)
                    vpi = item_do.get("videoPlayInfo", {})
                    if isinstance(vpi, dict):
                        _add_img(vpi.get("url", ""))
                    for img in item_do.get("imageInfos", []):
                        if isinstance(img, dict):
                            _add_img(img.get("url", ""))
                    if not images:
                        dp = item_do.get("defaultPicture")
                        if isinstance(dp, str) and dp.startswith(("http://","https://","//")):
                            _add_img(dp)

                    desc = item_do.get("desc", "") or " "
                    title_new = item_do.get("title", "")

                    brief_parts = []
                    for cpv in item_do.get("cpvLabels", []):
                        if isinstance(cpv, dict) and cpv.get("propertyName") and cpv.get("valueName"):
                            brief_parts.append(f"{cpv['propertyName']}：{cpv['valueName']}")
                    brief = "\n".join(brief_parts)

                    # 淘寶分類 (category = catId|channelCatId|catName|tbCatId)
                    cat_name = ""
                    channel_cat_id = ""
                    for lbl in item_do.get("itemLabelExtList", []):
                        props = lbl.get("properties", "")
                        if "分类:" in props and "##" in props:
                            parts = props.split("##")
                            if len(parts) >= 3:
                                cat_name = parts[-1]
                            channel_cat_id = str(lbl.get("channelCateId", ""))
                            break
                    cat_dto = item_do.get("itemCatDTO", {}) or {}
                    cat_id = str(cat_dto.get("catId", "") or item_do.get("categoryId", ""))
                    tb_cat_id = str(cat_dto.get("tbCatId", "") or "")
                    if not channel_cat_id:
                        channel_cat_id = str(cat_dto.get("channelCatId", ""))
                    category_full = f"{cat_id}|{channel_cat_id}|{cat_name}|{tb_cat_id}"
                    cat_dto_json_str = json.dumps(cat_dto, ensure_ascii=False) if cat_dto else ""

                    # seller 擴展
                    seller_do = data.get("sellerDO", {}) or {}

                    sets = ["description=?", "brief=?"]
                    params = [desc, brief]
                    if images:
                        sets.append("images=?"); params.append(json.dumps(images, ensure_ascii=False))
                    if category_full.count("|") >= 3:
                        sets.append("category=?"); params.append(category_full)
                    if cat_dto_json_str:
                        sets.append("cat_dto_json=?"); params.append(cat_dto_json_str)
                    if cat_id:
                        sets.append("category_id=?"); params.append(cat_id)
                    leaf_id = str(cat_dto.get("leafId", "") or "")
                    if leaf_id:
                        sets.append("leaf_id=?"); params.append(leaf_id)
                    if seller_do.get("city"):
                        sets.append("seller_city=?"); params.append(seller_do.get("city", ""))
                    if seller_do.get("nick"):
                        sets.append("seller_nick=?"); params.append(seller_do.get("nick", ""))
                    if seller_do.get("hasSoldNumInteger"):
                        sets.append("seller_sold_count=?"); params.append(int(seller_do.get("hasSoldNumInteger", 0) or 0))
                    if seller_do.get("itemCount"):
                        sets.append("seller_item_count=?"); params.append(int(seller_do.get("itemCount", 0) or 0))
                    if seller_do.get("newGoodRatioRate"):
                        sets.append("seller_good_rate=?"); params.append(str(seller_do.get("newGoodRatioRate", "")))
                    if seller_do.get("lastVisitTime"):
                        sets.append("seller_last_active=?"); params.append(str(seller_do.get("lastVisitTime", "")))
                    if title_new:
                        sets.append("title=CASE WHEN title='' OR title IS NULL THEN ? ELSE title END")
                        params.append(title_new)
                    if detail_price:
                        sets.append("price=?")
                        params.append(detail_price)
                    if detail_time:
                        sets.append("post_time=?")
                        params.append(str(detail_time))

                    # sellerDO 字段
                    seller_do = data.get("sellerDO", {}) or {}
                    if seller_do:
                        sid = str(seller_do.get("sellerId", ""))
                        if sid:
                            sets.append("seller_id=?")
                            params.append(sid)
                        snick = seller_do.get("nick", "")
                        if snick:
                            sets.append("seller_nick=?")
                            params.append(snick)
                        savatar = seller_do.get("avatar", seller_do.get("avatarUrl", ""))
                        if savatar:
                            sets.append("seller_avatar=?")
                            params.append(savatar)
                        loc = seller_do.get("city", seller_do.get("publishCity", ""))
                        if loc:
                            sets.append("location=?")
                            params.append(loc)

                    # 分类: catId|channelCatId|细分名称|tbCatId
                    cat_name = ""
                    channel_cat_id = ""
                    for lbl in item_do.get("itemLabelExtList", []):
                        props = lbl.get("properties", "")
                        if "分类:" in props and "##" in props:
                            parts = props.split("##")
                            if len(parts) >= 3:
                                cat_name = parts[-1]
                            channel_cat_id = str(lbl.get("channelCateId", ""))
                            break
                    cat_dto = data.get("itemDO", {}).get("itemCatDTO", {})
                    cat_id = str(cat_dto.get("catId", "") or item_do.get("categoryId", ""))
                    tb_cat_id = str(cat_dto.get("tbCatId", "") or "")
                    if not channel_cat_id:
                        channel_cat_id = str(cat_dto.get("channelCatId", ""))
                    cat = f"{cat_id}|{channel_cat_id}|{cat_name}|{tb_cat_id}"
                    if cat != "||":
                        sets.append("category=?")
                        params.append(cat)
                    cond = item_do.get("stuffStatus", "")
                    if cond:
                        sets.append("condition=?")
                        params.append(cond)
                    want = int(item_do.get("wantCnt", 0) or 0)
                    if want:
                        sets.append("want_count=?")
                        params.append(want)
                    view = int(item_do.get("browseCnt", 0) or 0)
                    if view:
                        sets.append("view_count=?")
                        params.append(view)

                    params.append(item_id)
                    self.conn.execute(f"UPDATE products SET {', '.join(sets)} WHERE item_id=?", tuple(params))
                    self.conn.commit()
                    enriched += 1
                    self._detail_logs.append(f"[保存] {item_id}")

            completed = i + 1

            # 连续50条时间过期 → 后面更老, 批量清除
            if self.publish_days and consecutive_expired >= 50:
                remaining_ids = [rid for rid in all_ids[completed:] if rid not in results or results.get(rid)]
                remaining_ids = all_ids[completed:]
                if remaining_ids:
                    self._log(f"  [验证] 连续{consecutive_expired}条超出{self.publish_days}天, "
                              f"剩余{len(remaining_ids)}条更早, 批量清除")
                    ph = ','.join('?' * len(remaining_ids))
                    self.conn.execute(f"DELETE FROM products WHERE item_id IN ({ph})", remaining_ids)
                    self.conn.commit()
                    removed += len(remaining_ids)
                    removed_filter += len(remaining_ids)
                    for rid in remaining_ids:
                        self._detail_logs.append(f"[时间过滤] {rid}")
                break

            if completed % 100 == 0 or completed == total:
                elapsed = time.time() - start
                speed = completed / elapsed * 60 if elapsed > 0 else 0
                self._log(f"  验证进度 {completed}/{total}, 补全{enriched}条, 移除{removed}条, "
                          f"跳过{skipped}条 ({speed:.0f}条/分)")
            if completed % 20 == 0 or completed == total:
                self._progress(total + completed, total * 2, f"验证 {completed}/{total} 补全{enriched}")

        details = []
        if removed_sold:
            details.append(f"已售/下架{removed_sold}")
        if removed_filter:
            details.append(f"价格/时间过滤{removed_filter}")
        detail_text = f" ({', '.join(details)})" if details else ""

        self._log(f"[验证] 完成: 补全{enriched}条, 移除{removed}条{detail_text}"
                  + (f", 跳过{skipped}条" if skipped else ""))
        return removed, enriched

    def download_images(self, save_dir: str, limit: int = 0, max_workers: int = 16) -> int:
        self._stop = False
        save_path = Path(save_dir)
        save_path.mkdir(parents=True, exist_ok=True)

        try:
            max_workers = int(max_workers)
        except (ValueError, TypeError):
            max_workers = 16
        max_workers = max(1, min(max_workers, 32))

        query = "SELECT item_id, title, images FROM products WHERE images != '[]' AND images != ''"
        if limit > 0:
            query += f" LIMIT {limit}"
        rows = self.conn.execute(query).fetchall()

        if not rows:
            self._log("[下载] 没有可下载的图片")
            return 0

        tasks = []
        item_ids_with_tasks = set()
        skipped_existing = 0

        for item_id, title, images_json in rows:
            try:
                images = json.loads(images_json) if images_json else []
            except (json.JSONDecodeError, TypeError):
                continue
            if not images:
                continue

            item_dir = save_path / str(item_id)
            item_dir.mkdir(exist_ok=True)

            for i, url in enumerate(images):
                if not url or not isinstance(url, str):
                    continue

                parsed = urlparse(url)
                ext = Path(parsed.path).suffix or ".jpg"
                if ext not in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"):
                    ext = ".jpg"

                filepath = item_dir / f"{i + 1}{ext}"
                if filepath.exists() and filepath.stat().st_size > 1000:
                    skipped_existing += 1
                    continue

                if url.startswith("//"):
                    url = "https:" + url
                elif url.startswith("http://"):
                    url = url.replace("http://", "https://", 1)

                tasks.append((item_id, url, str(filepath)))
                item_ids_with_tasks.add(item_id)

        if not tasks:
            self._log(f"[下载] 所有图片已存在 (跳过{skipped_existing}张), 无需下载")
            self._progress(1, 1, "完成")
            return 0

        self._log(f"[下载] 共 {len(item_ids_with_tasks)} 个商品 {len(tasks)} 张图片待下载"
                  f" (跳过{skipped_existing}张已存在, 线程{max_workers})")

        _local = threading.local()

        def _get_dl_session():
            if not hasattr(_local, "session"):
                _local.session = curl_requests.Session(impersonate=IMPERSONATE)
            return _local.session

        def _download_one(task, attempts=2):
            item_id, url, filepath = task
            if self._stop:
                return item_id, False, task

            path_obj = Path(filepath)
            for attempt in range(1, attempts + 1):
                if self._stop:
                    return item_id, False, task
                try:
                    sess = _get_dl_session()
                    # Force JPG via Accept header — APP 返回的 .heic URL 會被 CDN 轉成 JPG 給我們
                    headers = {"Accept": "image/jpeg,image/png,image/webp,image/*;q=0.8"}
                    resp = sess.get(url, timeout=20, headers=headers)
                    if resp.status_code == 200 and len(resp.content) > 500:
                        tmp_path = path_obj.with_suffix(path_obj.suffix + ".part")
                        with open(tmp_path, "wb") as f:
                            f.write(resp.content)
                        tmp_path.replace(path_obj)
                        return item_id, True, task
                except Exception:
                    pass

                if attempt < attempts:
                    time.sleep(random.uniform(0.2, 0.6) * attempt)

            return item_id, False, task

        downloaded_items = set()
        completed = 0
        total_downloaded = 0
        start = time.time()

        def _run_batch(batch_tasks, workers, attempts, update_progress=False):
            nonlocal completed, total_downloaded
            failed = []

            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [pool.submit(_download_one, t, attempts) for t in batch_tasks]

                for future in as_completed(futures):
                    if self._stop:
                        pool.shutdown(wait=False, cancel_futures=True)
                        break

                    item_id, success, task = future.result()
                    if success:
                        downloaded_items.add(item_id)
                        total_downloaded += 1
                    else:
                        failed.append(task)

                    if update_progress:
                        completed += 1
                        if completed % 10 == 0 or completed == len(tasks):
                            elapsed = time.time() - start
                            speed = total_downloaded / elapsed if elapsed > 0 else 0
                            self._progress(completed, len(tasks),
                                           f"{len(downloaded_items)}个商品 {total_downloaded}张 {speed:.0f}张/s")
                        if completed % 100 == 0:
                            elapsed = time.time() - start
                            speed = total_downloaded / elapsed if elapsed > 0 else 0
                            self._log(f"  下载进度 {completed}/{len(tasks)} 张, "
                                      f"{len(downloaded_items)} 个商品, {speed:.0f}张/秒")
            return failed

        failed_tasks = _run_batch(tasks, max_workers, attempts=2, update_progress=True)

        if failed_tasks and not self._stop:
            retry_workers = max(4, max_workers // 2)
            self._log(f"[下载] 首轮失败 {len(failed_tasks)} 张，降速重试(线程{retry_workers})...")
            failed_tasks = _run_batch(failed_tasks, retry_workers, attempts=3, update_progress=False)

        elapsed = time.time() - start
        speed = total_downloaded / elapsed if elapsed > 0 else 0
        fail_note = f", 失败{len(failed_tasks)}张" if failed_tasks else ""
        self._log(f"[下载] 完成: {len(downloaded_items)}个商品 {total_downloaded}张图片, "
                  f"耗时{elapsed:.1f}s ({speed:.0f}张/秒), 跳过{skipped_existing}张已存在{fail_note}")
        self._progress(len(tasks), len(tasks), "完成")
        return len(downloaded_items)
