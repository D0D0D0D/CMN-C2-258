# Test Specification — CMN-C2-258

## Test Strategy
- Coverage target: 主要パス 100%（全テストはネットワーク・実 DB・実 LLM 非依存。fake 注入でオフライン実行）
- Test types: Unit（services / nodes）/ Integration（外側 backbone E2E）/ Proof-of-Boundary

## Framework Compliance Tests (Mandatory)

| TC-ID | Test | Expected Result | Result |
|-------|------|----------------|--------|
| TC-01 | State contract: flat TypedDict | Type check pass, no Pydantic/dataclass | PASS（`tests/proof_of_boundary/test_state_safety.py`） |
| TC-02 | SecurityViolationError fires on invalid input | Error raised | PASS（SSRF ガード: `test_web_research_service.py::TestSsrfGuard`） |
| TC-03 | No JWT/Credential in State | CI の資格情報スキャン: 0 violations（S-5 はソースの静的検査として CI 側で実施） | PASS（CI 緑） |
| TC-04 | InvocationContext via configurable only | Direct access raises error | PASS（State 非格納・`test_state_safety.py`） |
| TC-05 | S-4: no duplicate lifecycle events in `execute()` | `node_start` / `node_complete` / `node_error` absent from `execute()` body | 0 duplicates |
| TC-06 | S-2: `_security_gate_input()` not overridden (`FunctionNode` subclass) | `TypeError` raised at class definition if overridden (`@final` enforced by framework) | 0 overrides（拡張は `_extra_security_gate_input()`） |
| TC-07 | S-3: `_security_gate_output()` not overridden (`FunctionNode` subclass) | `TypeError` raised at class definition if overridden (`@final` enforced by framework) | 0 overrides（拡張は `_extra_security_gate_output()`） |
| TC-08 | `required_trust_level` declared and enforced — dedicated S-1 boundary coverage MUST call `node(state)` (via `__call__`) for every privileged node, or use equivalent parameterized coverage | Insufficient trust level → execution refused *before* `execute()`; test must prove `execute()` was not called. Direct `node.execute(state)` is permitted only for isolated business-logic unit tests: it does not satisfy TC-08, but is not a finding when TC-08 and PB-6 coverage pass. | PASS（全ノード VERIFIED_EXTERNAL 宣言・`test_pb_outer_backbone_security.py` + `test_pb_invoke_order.py::test_s1_denial_refuses_execution_before_execute`） |
| TC-09 | S-2: `_extra_security_gate_input()` non-trivial when domain checks needed | Domain-specific input checks execute correctly (e.g. PII scan on additional fields, consent validation, business rules) | PASS（入力サイズ上限 + `loss_reason` PII マスク） |
| TC-10 | S-3: `_extra_security_gate_output()` non-trivial when domain checks needed | Domain-specific output checks execute correctly (e.g. nested credential scan, PII re-check, content filtering, preservation verification) | PASS（PII scrub + 丸写しクリップ + 勝率サンプル数検査） |
| TC-11 | S-4: at least one domain `emit_trace_event()` inside each `execute()` | Domain event emitted on every invocation path | ≥1 per node（全 execute 実装で emit） |

## Proof-of-Boundary Tests (Mandatory)

| PB-ID | Boundary | Test | Expected Result | Result |
|-------|----------|------|----------------|--------|
| PB-1 | BaseNode → EventEmitter | `emit_trace_event()` fires on every invocation path | No silent failures | PASS |
| PB-2 | State serialization | Post-invoke State is primitives only | No Pydantic/dataclass | PASS（`test_state_safety.py` + GraphNode JSON パック `default=str`） |
| PB-3 | L1 → External service | 外部サービス接続は注入シーム経由（DB=rows/BaseDatabase・Web=fetcher・LLM=BaseLLM） | fake 注入で実データフロー検証 | PASS（`test_end_to_end.py`） |
| PB-4 | Import isolation | No Level 0 imports | AST scan: 0 violations | PASS（`test_import_isolation.py`） |
| PB-5 | Checkpoint safety | No JWT/Pydantic in checkpoint | Inspection pass | PASS（credential は `ctx.secrets` 経由・State 非格納） |
| PB-6 | Invoke execution order | `__call__()`: S-1 trust gate → S-4 `node_start` → S-2 `_security_gate_input` → `execute()` → S-3 `_security_gate_output` → S-4 `node_complete` | Order verified | PASS（`test_pb_invoke_order.py` — GraphNode は fake subgraph 注入で order を検証） |
| PB-7 | Cat-2 外側 backbone セキュリティ | 外側 entry/exit ノードの S-1 宣言・S-2 入力バウンド・S-3 出力検査、GraphNode の S-2/S-3 deliberate no-op（ADR-017） | Gate 実効性 verified | PASS（`test_pb_outer_backbone_security.py`） |

## Business Logic Tests

| TC-ID | Test | Input | Expected Result | Result |
|-------|------|-------|----------------|--------|
| BL-01 | Contract 1.0.0 準拠検証 — 必須列欠損の隔離 | `record_id`/`competitor`/`outcome`/`close_date` 欠損行 | `missing_map` に理由つき隔離・エラーで落とさない | PASS（`test_win_loss_repository.py`） |
| BL-02 | enum 外決着（cancelled 等）の隔離 | `outcome="cancelled"` 行 | テーブル存在許容・勝率集計から除外（Contract §5） | PASS |
| BL-03 | 表記ゆれ outcome の正規化 | `受注`/`失注`/`win`/`loss` | won/lost へ正規化 | PASS |
| BL-04 | 取得境界 — 競合×直近 N 年×max_records | 上限超過の rows | `close_date` 降順打ち切り + truncated フラグ + レポート明示 | PASS |
| BL-05 | 金額バンド近接（同一/隣接バンド） | ctx 4,500万 vs 記録 7,000万 / 2億 | 隣接=採用・2 バンド差=除外（バンド定義は config） | PASS（`test_aggregate.py`） |
| BL-06 | 段階フォールバック（緩和の可視化） | 完全一致 < min_sample | fallback_order 末尾から緩和・`relaxed_filters` に記録・レポート明示 | PASS |
| BL-07 | 勝率のサンプル数必須付与・僅少時は参考値 | 3 件未満のマッチ | `参考値（サンプル僅少）`・N%（M件中K勝）表記 | PASS |
| BL-08 | 欠損許容（類似性フィルタ列欠損の行をスキップしない） | industry 欠損行 | その列だけフィルタ除外・集計に含める | PASS |
| BL-09 | Web 収集の作法 — robots 遵守 | robots.txt Disallow パス | 収集しない（`disallowed by robots.txt`） | PASS（`test_web_research_service.py`） |
| BL-10 | Web 収集の作法 — SSRF ガード | loopback / メタデータ IP / 非 http(s) | `SecurityViolationError`（per-page では隔離） | PASS |
| BL-11 | Web 収集の作法 — レート制限・上限・出典記録 | 同一ドメイン連続アクセス | ドメイン単位 sleep・max_pages 打ち切り・URL+取得日時記録 | PASS |
| BL-12 | 事実抽出のみ保存（本文丸写し防止） | 長文 title/heading | `fact_max_chars` にクリップ・本文全文は State に持たない | PASS |
| BL-13 | Web 部分失敗の許容 | 一部 URL 失敗 / 全 URL 失敗 | 取得できた範囲で継続・失敗をレポート明示（収集ゼロでも社内勝敗のみで成立） | PASS（`test_end_to_end.py`） |
| BL-14 | LLM 縮退（synthesis 任意入力） | LLM 未設定 / LLM 例外 | status=success のまま決定論部のみのレポート + 省略明示 | PASS |
| BL-15 | S-3 — 勝率サンプル数注記の機械検査 | サンプル数なしの `N%` 行 | 該当行を非表示化（redact） | PASS |
| BL-16 | S-3 — 収集本文の丸写しクリップ | `quote_max_chars` 超の verbatim 引用 | クリップ（軽微利用ガード） | PASS |
| BL-17 | S-3 — 生レコード非掲載 | `deal_name` を含む rows | レポートに deal_name が現れない（集計値のみ） | PASS |
| BL-18 | S-2 — `loss_reason` PII マスク | email 入り loss_reason | State 格納前にマスク（LLM に渡らない） | PASS |
| BL-19 | Contract 準拠データ不在は稼働対象外 | 準拠行 0 件 | status=error + 要スキーマ確認メッセージ（公開情報のみの運用は対象外） | PASS |
| BL-20 | GraphNode JSON パック契約 | date 型含む records | `default=str` 直列化・`merge_output` は変更キーのみ返却・subgraph キャッシュ | PASS（`test_nodes.py::TestGraphNodeContract`） |
| BL-21 | リダイレクト hop ごとの再検証（SSRF/robots/レート） | 302 → メタデータ IP / Disallow パス / ループ | hop ごとに遮断・robots 再判定・max 3 hops 打ち切り（http→https 等の正当なリダイレクトは追従） | PASS（`TestRedirectGuard`） |
| BL-22 | 全行 Contract 非準拠は確認サマリで継続（経路(b) ドリフト防御） | 生行あり・全行違反 | error にせず 🛑 Contract 準拠確認をレポート冒頭に明示・データ品質ノートに隔離件数 | PASS |
| BL-23 | S-4 エラー分岐でも emit | 空入力 / 競合なし / DB 失敗 / データ不在 | 各分岐で domain event 発火 | PASS（`test_s4_emitted_on_error_paths`） |

## Test Execution Summary
- Execution date: 2026-07-03
- Total tests: 88
- Pass: 88 / Fail: 0 / Skip: 0
- Coverage: 主要パス網羅・全テストオフライン実行（DB/Web/LLM は fake 注入）。実装レビュー F-01〜F-05（リダイレクト SSRF/確認サマリ/品質ノート/S-4 エラー分岐/検証テスト）反映済み
