"""商品處理中樞 HTTP API (port 7778, P0)。

對外端點(對齊 XDZHGL 7777 規範):
  GET  /api/version              版本 + api_compat_version
  GET  /api/health               server 自身存活探測
  POST /api/hub/etl/run          非同步啟動 ETL → {job_id}
  GET  /api/hub/etl/status/{id}  輪詢 job 狀態 + step 進度
  POST /api/hub/keyword_filter/check  批次違規詞檢查
  POST /api/hub/auto_mapper/infer     批次未映射分類自動推斷

不改現有任何 .py / .json / .xlsx,所有新檔都在 api/ 下。
規則檔 read-only 讀,P0 不寫盤。
"""

API_COMPAT_VERSION = "1"
API_VERSION = "1.0"
