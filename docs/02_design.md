# Template Design Specification

## Position in AgentCore Architecture

- **Agent Class**: CompetitiveAnalysisWinRateEstimationGraph
- **L1 Base**: AgentBaseGraph（**L1 Base** を直接継承。AutonomousBaseGraph は使わない）
- **Three-Layer Separation**:
  - State: flat TypedDict composition (no Pydantic — msgpack incompatible)
  - Node: L1 inheritance (Template Method: `execute(self, state: dict) -> dict` override only)
  - Graph: composition (`register_nodes()` for node substitution)

本テンプレは Cat 2 オーケストレーターであり、外側は `AgentBaseGraph` の固定 5 ノード backbone
（initialize → pre_process → main → post_process → finalize）に従う。ドメインのマルチステップ
（競合公開情報の Web 収集・社内勝敗の決定論集計・LLM 統合分析）は `main` スロットに置いた `GraphNode` が
内側の `domain_workflow_graph`（`src/graph/domain_workflow_graph.py`）として encapsulate する。旧 Level 2 base
継承モデル（RAGAgent 等の L2 型を継承する方式）は本リポジトリでは廃止され、**L1 Base を直接継承**する。

## Architecture Overview

### Node Configuration

| Node | Responsibility | Input State | Output State | Inherits/Overrides |
|------|---------------|-------------|--------------|-------------------|
| initialize | schema_version / session_id / trust_level を設定 | — | メタ初期化 | InitializeNode (default) |
| pre_process | `user_input` から競合製品名・商談コンテキスト（業種/商材/金額レンジ）を抽出。`shared.services.database` で共通 DB `win_loss` テーブルを fetch（読み取り専用・欠損許容・**対象競合＋直近 N 年でクエリ段階から絞り込み、上限 `max_records` 件**）。集計に不要な PII カラムを State 格納前にドロップ | `user_input`, `input_context`（任意: `competitor_urls` のみ） | `competitors`, `deal_context`, `win_loss_records`, `missing_map` | FunctionNode（`execute` + `_extra_security_gate_input()` override） |
| main | 内側 `domain_workflow_graph` を実行（下記 3 サブノード）。GraphNode として上流 `status="error"` はパススルー | `competitors`, `deal_context`, `win_loss_records` | `competitor_intel`, `win_rate_stats`, `synthesis` | GraphNode（`get_subgraph()` / `extract_input()` / `merge_output()` override） |
| post_process | S-3 出力チェック（社外秘漏洩防止・収集本文の丸写し排除・勝率のサンプル数/確度明示検査）・Markdown 競合比較レポート生成。**`synthesis` は任意入力**: LLM 失敗で欠落している場合は決定論部（比較素材 + 勝率統計）のみのレポートを生成し、LLM 分析欠落を明示する | `competitor_intel`, `win_rate_stats`, `synthesis`（任意） | `output`（Markdown） | FunctionNode（`execute` + `_extra_security_gate_output()` override） |
| finalize | response_metadata / total_time_ms を構築 | — | 応答メタ | FinalizeNode (default) |

#### 内側 domain_workflow_graph のサブノード（main の GraphNode が実行）

| サブノード | 種別 | 責務 |
|---|---|---|
| CompetitorResearchNode | Web 収集（決定論 I/O） | 先頭で `user_input` を `json.loads` して `competitors`/`deal_context`/`win_loss_records` を展開。競合ごとに公開製品ページ・プレス等を `src/services/web_research_service.py`（注入シーム）で収集。**robots.txt 遵守・レート制限・タイムアウト・収集上限 N 件・出典（URL+取得日時）記録・事実抽出のみ保存（本文全文は State に持たない）** |
| WinLossAggregateNode | 決定論 | 類似案件の抽出（`competitor` 一致 AND `industry` 一致 AND `amount` の金額バンド近接［バンド定義は本リポ config・契約はバンドを規定しない］ AND `close_date` が直近 N 年以内）→ 勝率 = 件数カウント（例: 5 件中 3 勝 = 60%）。**サンプル数を必ず付与**。3 件未満は「参考値（サンプル僅少）」。完全一致が少ない場合は条件を段階的に緩めて再集計し、**緩めた条件を明示**（段階フォールバック） |
| SynthesisNode | LLM | 公開情報 × 社内勝敗の突合せ: 機能比較表・差別化ポイントの生成、`loss_reason`（自由記述）の傾向分析（例「保守費用で負けるパターンが多い」）。LLM 呼び出しはこのノードに集約（コスト最小化） |

### Data Flow

```
START → initialize → pre_process → main → {route} → post_process → finalize → END
                                            ↓ (retry)
                                          pre_process

  [main 内側 domain_workflow_graph]
    CompetitorResearch → WinLossAggregate → Synthesis
```

> **route の契約（SDK 1.0.0）**: `pre_process → main` は無条件辺。`route()` は main 実行後に
> `state["status"]` を読む（`success`→post_process / `retry`→pre_process / 他→finalize）。
> `add_edges()` / `route()` は override しない。main は上流 `status="error"` を無条件でパススルーする
> （pre_process が DB 取得失敗で error を立てた場合、main の GraphNode は再実行せず error を通す）。

### State Definition

| Field | Type | Purpose | Required |
|-------|------|---------|----------|
| `input_context` | `dict` | 公開情報収集先の `competitor_urls`（任意）。これ以外のキーはドメインノードで消費しない | No |
| `competitors` | `list[str]` | 分析対象の競合製品名（pre_process で抽出） | Yes（main まで） |
| `deal_context` | `dict` | 今回商談のコンテキスト（業種・商材カテゴリ・想定金額。類似性フィルタに使用 — 金額は config のバンド定義で `win_loss.amount` と同一バンド化して比較） | Yes |
| `win_loss_records` | `list[dict]` | `win_loss` テーブルから fetch した勝敗記録（primitive のみ・PII ドロップ済） | Yes（main まで） |
| `missing_map` | `dict` | record_id → 欠損列リスト（類似性フィルタから除外した列の記録） | Yes |
| `competitor_intel` | `list[dict]` | Web 収集した競合公開情報（事実抽出 + 出典 URL + 取得日時） | Yes（post まで） |
| `win_rate_stats` | `dict` | 競合別の勝率統計（勝率・サンプル数・適用フィルタ・フォールバック段階・確度ラベル） | Yes（post まで） |
| `synthesis` | `dict` | LLM 統合分析（機能比較表・差別化ポイント・失注理由傾向） | No（LLM 失敗時は決定論部のみ出力） |
| `output` | `str` | 最終 Markdown 競合比較レポート | Yes（finalize） |
| `status` | `str` | AgentStatus 値（"success" / "error" / "retry"） | Yes |

**State Constraints (mandatory):**
- Flat TypedDict only (primitives + JSON-serializable types)
- No JWT, API keys, credentials in State (checkpoint DB leakage)
- InvocationContext via `config["configurable"]` only (not in State)
- No Pydantic models, dataclass, arbitrary Python objects (msgpack incompatible)

> **DB / Web 接続情報の扱い**: `win_loss` テーブルへの接続情報は `ctx.secrets.require("...")` 経由で取得し、
> **State には一切格納しない**（State は checkpoint DB へ永続化されるため、credential in State =
> credential in plaintext）。DB は**読み取り専用参照**で書き戻しの副作用を持たない。Web 収集も認証不要の
> 公開ページのみを対象とし、ログイン必須・有料の非公開情報は対象外。

## AGENTIC STAR Marketplace Entry

リポジトリ直下の `cli.py` は Marketplace runner と本 AG の入力・secret 契約を接続する専用 adapter である。

- `MarketplaceGraph` は既存 `CompetitiveAnalysisWinRateEstimationGraph` を継承する。runner が渡す
  `input_context` はマージせず破棄し、AG が実際に読むキーだけの `{"competitor_urls": {}}` を新規構築する。
  `conversation_history` など S-2 対象外の自由文は State に入れない。収集 URL を指定する場合は、検証対象である
  `user_input` の JSON payload 内に `competitor_urls` を含める。
- 起動時の必須値は `AZURE_OPENAI_API_KEY` / `AZURE_OPENAI_ENDPOINT` /
  `AZURE_OPENAI_DEPLOYMENT`。adapter の allowlist provider が process environment からこの 3 値だけを公開し、
  未設定・空文字・空白のみを `provision_secrets()` で拒否する。
- Azure LLM は `config["llm"]` から `CompetitiveAnalysisGraphNode._injected` → `_parent_config()` →
  `CompetitiveAnalysisWorkflow(config=...)` → `SynthesisNode(llm=...)` の既存注入シームだけを通す。
  Azure client は呼び出しごとに `current_secrets()` から接続値を解決し、client や secret をインスタンスに保持しない。
- ローカル AgentCore 1.0.1 には Marketplace の `shared.bootstrap` と Azure client module がないため、両 import は
  実行箇所まで遅延する。`load_agent_config` は 1.0.1 の `load_config` へフォールバックし、`import cli` は
  1.0.1 / Marketplace 側（1.0.2 系を想定・要確認）の双方で成立させる。

adapter は勝敗データを追加・補完しない。`win_loss_rows` のデモ値も注入せず、準拠データ 0 件なら既存の
`PreProcessNode` が error にする。

### 事実層と LLM 推論層の境界

Marketplace 化後も、勝率の分子・分母・率は `WinLossAggregateNode` が Contract 準拠レコードから決定論的に
算出した `win_rate_stats` だけを正本とする。`SynthesisNode` はその統計を変更せず、LLM 応答から
`comparison_table_md` / `differentiators` / `loss_patterns` の 3 キーだけを抽出する。最終段の
`PostProcessNode._extra_security_gate_output()` は `verify_sample_annotation()` を再実行し、サンプル数注記のない
勝率表記を非表示化する。adapter はこのデータフローや出力 allowlist を変更せず、LLM に勝率を算出させる経路を
追加しない。

### `win_loss` テーブル・インターフェース仕様（本 AG が要求する読み取りスキーマ）

> 本 AG が勝率推定に必要とする **`win_loss` テーブルの列**を定義する。これは営業支援クラスタの
> **共通 Data Contract（正本 = Enterprise Sales Support Data Foundation Builder Agent リポ `docs/data_contract.md`・contract_version 1.0.0）** への参照側
> インターフェース要件であり、契約の相手は Data Contract（スキーマ）であって Enterprise Sales Support Data Foundation Builder Agent という AG 実装
> ではない（2026-07-03 クラスタ方針確定）。テーブルの整備経路は 2 つを許容する —
> **経路(a)**: 基盤 AG Enterprise Sales Support Data Foundation Builder Agent が非定型記録を分類・正規化して整備（任意のセットアップヘルパー）、
> **経路(b)**: 顧客が Data Contract 準拠の `win_loss` テーブルを直接用意（既存 SFA/CRM エクスポート等・
> Enterprise Sales Support Data Foundation Builder Agent 不要）。いずれの経路でも本 AG の動作は同一（列名ズレは pre_process の列名マッピング
> config で吸収する）。
> 本 AG は **`shared.services.database.BaseDatabase.fetch_many()`** で読み取り、DB エンジンは MySQL /
> PostgreSQL いずれも可（`mysql_client` / `postgres_client` が 1.0.0 wheel に実在）。
>
> **欠損許容の原則**: 類似性フィルタ列（`industry` / `product_category` / `amount`）が欠けている行は
> スキップせず、その列だけフィルタ条件から除外して集計に含める（`missing_map` に記録し、レポートで
> 「フィルタ精度低下」を明示）。必須は**集計が成立する最小限**（`record_id` / `competitor` / `outcome` /
> `close_date`）のみ。
>
> **取得境界（サイズ暴走防止）**: fetch は全件取得ではなく、**クエリ段階で対象競合（`competitor IN (...)`）
> ＋直近 N 年（`close_date >=`）に絞り、`max_records` 件（config・既定 500・`close_date` 降順）で打ち切る**。
> 打ち切りが発生した場合は `missing_map` とレポートに「対象期間内の全件ではない」ことを明示する。これにより
> State・GraphNode の JSON パック・checkpoint のサイズが常に有界であることを保証する。

| 列名（正規化後） | 型 | 用途 | 必須 | 欠損時の扱い |
|---|---|---|---|---|
| `record_id` | str/int | 勝敗記録の一意キー（`missing_map` のキー） | **必須** | 欠損行はスキップ（識別不能のため） |
| `deal_name` | str | 案件名（**内部トレース用のみ**: `missing_map` / S-4 監査ログでのサンプル根拠参照。**レポートには掲載しない** — S-3 の生レコード列挙禁止と一本化） | 任意 | 内部トレースで `record_id` のみ使用 |
| `competitor` | str | 競合名。**集計キー**（勝率推定の第一フィルタ） | **必須** | 欠損行は集計対象外（競合を特定できないため） |
| `outcome` | enum (won/lost) | 勝敗。勝率計算の分子/分母 | **必須** | 欠損行は集計対象外 |
| `industry` | str | 業種（類似性フィルタ） | 任意 | フィルタから除外・`missing_map` 記録 |
| `product_category` | str | 商材カテゴリ（類似性フィルタ） | 任意 | 同上 |
| `amount` | int | 金額（円に正規化・類似性フィルタ）。**バンド化（レンジ割り）は本リポ `config/agent.yaml` のバンド定義で行い、近接 = 同一/隣接バンドで判定**（契約はバンドを規定しない — Contract 1.0.0 R-1 案 A） | 任意 | 同上 |
| `close_date` | date | 決着日（直近 N 年フィルタ・鮮度の重み） | **必須** | 欠損行は集計対象外（鮮度不明の戦績は誤導リスク） |
| `loss_reason` | text | 失注理由の自由記述（SynthesisNode の LLM 分析入力） | 任意 | LLM 傾向分析から除外（勝率集計には影響しない） |

> **列名マッピング**: 実テーブルの列名が上表と異なる場合、`config/agent.yaml` の `column_map`
> （`{正規化名: 実列名}`）で吸収し、コード変更なしで合わせられる設計とする（経路(a)/(b) どちらの整備でも
> 同じ仕組みで適合させる）。これにより win_loss スキーマの最終確定が正本側で遅れても、本 AG の実装・
> テスト（fake DB 注入）は先行できる。
> ※ 本仕様は Data Contract version 1.0.0 と整合する — `outcome` enum は won/lost に統一・金額列は
> `amount`（int・円正規化・バンド化は消費側 config）・`deal_name` は NULL 可（内部トレース用）。
> Data Contract の公開済み改版に応じて本仕様も更新する。
>
> **経路(b) ドリフト防御（Contract 準拠検証）**: pre_process は `column_map` 適用後に (1) 必須列
> （`record_id`/`competitor`/`outcome`/`close_date`）の存在、(2) `outcome` の enum 値（won/lost への
> 正規化可否）、(3) `close_date` の日付パース可否、(4) `amount` の数値化可否（数値化不能は欠損扱い）を
> 検証する。違反行は `missing_map` に理由つきで計上して集計から除外し、**全行が違反の場合は「Contract
> 非準拠の可能性・要スキーマ確認」を明示した確認サマリを返す**（エラーで落とさない）。enum 外の決着
> レコード（中止・延期・保留等）は契約上テーブル存在が許容されるため（Contract 1.0.0 §5）、エラーでは
> なく隔離で扱い勝率集計から除外する。未知列は無視する（前方互換）。これにより Enterprise Sales Support Data Foundation Builder Agent の品質検証を経ない
> 経路(b) データでも、不正データが勝率集計へ混入する前に検出・隔離される。
>
> **PII カラムの扱い（S-2 連携）**: `win_loss` が顧客担当者名・email・電話等の PII カラムを含む場合、
> pre_process は集計に不要な PII を **State 格納前にドロップ**し、上表の集計因子と `loss_reason` /
> 内部トレース用 `deal_name`（レポート非掲載・S-3 の生レコード非掲載ポリシーに従う）だけを残す
> （本節の表に PII 連絡先列は含めない = 本 AG は要求しない）。残す
> `loss_reason` は S-2 スキャン対象とし、PII 検出時はマスクする。

## Framework Utilization

### Shared Components Used
- [x] InvocationContext (correlation_id, session_id, permissions, credential handle)
- [x] ConnectionPolicy (retry/timeout strategy)
- [x] SecurityViolationError
- [x] S-2: `_extra_security_gate_input()` — domain-specific input check hook
      pre_process で (a) `user_input`（競合名・商談コンテキスト）への注入的入力・過大サイズの検査、
      (b) DB から取り込む `win_loss` 行全体（`loss_reason` 自由記述を含む）への PII スキャンを実装する。
      既定の `user_input` / `validated_input` スキャンは DB 由来フィールドを見ないため、ドメイン拡張フックで
      **State へ格納する前・かつ内側グラフ／LLM へ渡す前**に走らせる。集計に不要な PII カラムは格納前に
      ドロップし、`loss_reason` 内の PII はマスクする。`_security_gate_input()` は override しない。
- [x] S-3: `_extra_security_gate_output()` — domain-specific output check hook
      post_process で Markdown レポートを出力する前に、(a) 既定 credential スキャンに加え、(b) **社外秘
      漏洩防止**: `win_loss` の生レコード（案件名・金額・顧客情報）をレポートへそのまま列挙しない（集計値・
      統計と傾向のみ）、(c) **収集本文の丸写し排除**: `competitor_intel` の収集テキストから連続長文引用
      （閾値 config 化）がレポートに混入していないか検査（著作権法 47 条の 5 の「軽微利用」逸脱防止）、
      (d) **勝率の確度明示検査**: レポート中のすべての勝率数値にサンプル数（N 件中 M 勝）が併記されている
      ことを機械検査する。`_security_gate_output()` は override しない。
- [x] S-4: `emit_trace_event()` — at least one domain-specific event inside each `execute()`
      pre_process の DB fetch、CompetitorResearchNode の Web 収集（収集先 URL 単位）、SynthesisNode の
      LLM 呼び出しなど、外部 I/O を伴う操作ごとに `from shared.utils.audit_logger import emit_trace_event`
      を `execute()` 内で呼ぶ。副作用のないノードでも最低 1 イベントを発火する。
      `node_start` / `node_complete` / `node_error` は `BaseNode.__call__()` が自動発火するため重複させない。

> **S-2/S-3 gate behaviour by node type (ADR-017):**
> - `FunctionNode` subclass → framework `@final` gate always runs automatically;
>   extend via `_extra_security_gate_input()` / `_extra_security_gate_output()` only
> - `GraphNode` / `RemoteAgentNode` → deliberate no-op (upstream or remote node's gate already applied)
> - Custom `BaseNode` subclass → must implement `_security_gate_input()` and
>   `_security_gate_output()` directly (`@abstractmethod` — omission raises `TypeError` at instantiation)

本テンプレでは pre_process / post_process を `FunctionNode` サブクラスとし、拡張は `_extra_*` フックで行う。
main は `GraphNode` のため S-2/S-3 は deliberate no-op（内側サブノードの gate と post_process の S-3 で担保）。

### Web 収集サービスの設計（`src/services/web_research_service.py`）

> ⚠️ **SDK 1.0.0 wheel の実機確認結果（2026-07-02）**: `shared.tools.web_search` は placeholder 実装
> （固定文字列を返すのみ）であり、Proposal 段階で想定した「プラットフォーム提供の safe_fetch」は
> **1.0.0 wheel に実在しない**。よって Web 収集は本テンプレ内のサービス層で自前実装する
> （`framework.llm` 不在時に `src/services/llm_client.py` を注入シームとして自前化するのと同じ方式）。
> プラットフォームへの safe_fetch 標準提供は将来の改善候補として別途扱う。

- **注入シーム**: `WebResearchService`（HTTP クライアントをコンストラクタ注入）。単体テストでは fake を注入し、
  ネットワークに一切出ない。CI もネットワーク非依存。
- **収集の作法（適法運用の実装要件）**: robots.txt の取得と遵守（Disallow パスは収集しない）/
  ドメイン単位のレート制限（既定 1 req/sec・config 化）/ タイムアウトと収集上限 N 件（config 化）/
  User-Agent の明示 / 出典（URL・取得日時）の記録 / 保存するのは**事実抽出（機能・価格帯・リリース情報等）
  のみ**で本文全文を State・レポートに持たない（著作権法 47 条の 5 の情報解析の範囲・軽微利用に収める）/
  ログイン必須・有料コンテンツは対象外。
- **接続失敗の扱い**: 一部 URL の収集失敗は許容し、収集できたソースのみで分析を継続する（収集ゼロ件の場合は
  「公開情報なし・社内勝敗のみのレポート」として成立させる。握りつぶさず取得失敗を S-4 で記録しレポートに明示）。

### Composition Pattern

- **Pattern**: GraphNode (subgraph) — main スロットに `DomainWorkflowGraphNode` を配置し、内側の
  `domain_workflow_graph`（Web 収集 → 決定論集計 → LLM 統合の 3 サブノードパイプライン）を wrap する
- **Composition target**: `src/graph/domain_workflow_graph.py`（内側 `AgentBaseGraph` / `BaseGraph`）
- **Error propagation strategy**: propagate — 内側サブノードで発生した error は `status="error"` として
  外側へ伝播し、main → post_process はパススルー、finalize で error 応答を返す。DB fetch 失敗・LLM 失敗は
  握りつぶさず伝播する（沈黙して誤らない設計）。ただし **Web 収集の部分失敗は error にしない**
  （上記のとおり収集できた範囲で継続し、失敗は明示する）

> **Cat2 GraphNode subgraph 契約（SDK 1.0.0 実機・データ受け渡しの具体）**: 内側グラフは `user_input`(str) +
> ctx のみを受領し、外側の任意 State キー（`competitors` / `deal_context` / `win_loss_records`）は自動伝播
> しない。このため本テンプレは以下の明示的な直列化／逆直列化の契約で構造化データを橋渡しする:
>
> 1. **外側→内側（送り）**: `DomainWorkflowGraphNode.extract_input(state)` を override し、pre_process が
>    構築した `competitors` / `deal_context` / `win_loss_records` / `missing_map` を **1 つの JSON 文字列**に
>    まとめて返す（`json.dumps({...}, default=str, ensure_ascii=False)`）。これが内側グラフの
>    `user_input`(str) になる（GraphNode は単一 `user_input` 文字列しか内側へ渡せないため、複数構造体は
>    1 つの JSON にパックする）。
> 2. **内側（先頭で復元）**: 内側グラフの先頭 `CompetitorResearchNode.execute()` で
>    `json.loads(state["user_input"])` し、各キーを内側 State へ展開する（以降の WinLossAggregate /
>    Synthesis は展開後のキーを直接参照）。
> 3. **内側→外側（戻し）**: `DomainWorkflowGraphNode.merge_output(state, sub_result)` を override し
>    （**SDK 1.0.0 実機の abstract メソッドは `merge_output(self, state, sub_result: dict) -> dict`**。
>    `assemble_output` という名称は存在しない）、内側が算出した `competitor_intel` / `win_rate_stats` /
>    `synthesis` を外側 State へマージした dict を返す（変更キーのみ返す。full state 返却は reducer で
>    二重化するため不可）。
>
> `default=str` は `close_date`（date 型）を安全に文字列化するため必須。JSON パック方式のため、
> `win_loss_records` は primitive のみ（flat・msgpack 互換）である前提を pre_process 側で保証する。
> パックサイズは pre_process の取得境界（対象競合＋直近 N 年＋`max_records` 上限）により常に有界。
> checkpointer は HITL 無しのため標準構成。

## Import Isolation Confirmation
- [x] Template does not import agenticstar-platform SDK (Level 0)
- [x] Import targets: framework/ and shared/ only (no agents/base/ required)

Level 0（`agenticstar` SDK）の直接 import は行わない。import は `framework.*`（AgentBaseGraph / GraphNode /
BaseNode / AgentState / InvocationContext / TrustLevel / SecurityViolationError）と `shared.*`
（`shared.services.database` / `shared.services.llm` / `shared.utils.audit_logger`）に限定する。
Web 収集の HTTP クライアント（requests 等）はテンプレの `pyproject.toml` に `==` 固定ピンで宣言する。

## Design Decision Record

| Decision | Option A | Option B | Chosen | Rationale |
|----------|----------|----------|--------|-----------|
| L1 base type | AgentBaseGraph | AutonomousBaseGraph | **AgentBaseGraph (L1 Base)** | 固定パイプライン型のマルチステップであり自律ループ不要。停止条件はパイプライン完了で決まる |
| Composition pattern | Standalone (3 slots) | GraphNode (subgraph) | **GraphNode (subgraph)** | Web 収集・決定論集計・LLM 統合のドメインワークフローを内側グラフに encapsulate し、外側 backbone を単純に保つ（クラスタ兄弟 Enterprise Deal Scoring & Pipeline Priority Agent と同型で横展開効率を確保） |
| Web 収集の実装 | `shared.tools.web_search`（FW 提供） | テンプレ内 `WebResearchService` 自前実装 | **テンプレ内自前実装（注入シーム）** | 1.0.0 wheel の `web_search` は placeholder で実収集不可（実機確認済）。robots 遵守・レート制限・出典記録の作法を含めてテンプレ内サービスで実装し、テストは fake 注入。FW への標準提供は将来の改善候補 |
| 勝敗データの持ち方 | KB (VectorRAG) 化して曖昧検索 | 構造化テーブルの決定論集計 | **決定論集計** | 「この競合との過去戦績」は `WHERE competitor=X` 相当の決定論フィルタで正確に引く。勝率は件数カウント。RAG の曖昧検索は勝率の根拠を毀損する |
| 勝率の提示 | 単一の勝率数値 | 勝率 + サンプル数 + 確度ラベル + フォールバック明示 | **確度つき提示** | 薄いサンプルで自信満々の数値を出さない（3 件未満は「参考値」）。フィルタを緩めた場合は緩和条件を明示。過信防止の compliance-by-construction |
| データ取り込みの責務 | 本 AG が非定型記録を取り込む | 外部整備の Contract 準拠データを参照のみ | **外部整備・参照のみ** | 単一責任。整備は経路(a) 基盤 AG Enterprise Sales Support Data Foundation Builder Agent（任意ヘルパー）または経路(b) 顧客直接用意のいずれでもよく、本 AG は Contract 準拠 `win_loss` を参照して競合分析に専念（リリースゲートは「Contract 準拠データの存在」で判定・Enterprise Sales Support Data Foundation Builder Agent 導入は問わない） |
| LLM の適用範囲 | 全ステップに LLM | 比較表ナラティブ・失注理由分析のみ | **限定適用** | 勝率は決定論で算出し LLM を挟まない（数値の再現性・監査可能性）。LLM は自由記述の傾向分析と表のナラティブ化のみ |

## 既知の前提・限界（レビュー時の論点・押印前確認）

- **依存の実体は「Data Contract 準拠の `win_loss` データの存在」であり、Enterprise Sales Support Data Foundation Builder Agent という AG ではない**:
  本 AG は `win_loss` テーブルを**読み取り参照**する。`shared.services.database` は FW 標準で技術的接続に
  ブロッカーはないため、**本 AG の実装・単体/PB テスト（fake DB 注入）は基盤整備と並行して進められる**。
  一方、**STG での実 invoke 動作確認・リリースには Contract 準拠の `win_loss` テーブルが実データで整備済み
  であることが前提**となる（整備経路は経路(a) Enterprise Sales Support Data Foundation Builder Agent / 経路(b) 顧客直接用意のいずれでも可 —
  リリースゲートで確認するのは**データの存在と Contract 準拠**であって Enterprise Sales Support Data Foundation Builder Agent の導入ではない）。
  したがって**本 AG は Enterprise Sales Support Data Foundation Builder Agent 無しでも単体導入できる**。なお経路(b) のデータには Enterprise Sales Support Data Foundation Builder Agent の
  品質検証（欠損/重複/型不整合・確度ゲート）が掛からないため、品質担保は顧客責務（本 AG 側の欠損許容
  原則・必須列スキップ・確度つき提示が防御線）。共通 DB に勝敗データが無い運用（公開情報のみ）は**対象外**（Proposal で
  必須要件として明記済み。公開情報のみでは汎用 LLM 代替可能となり moat が消失するため）。
- **Web 収集の適法性は「作法の実装」で担保する**: 公開情報の情報解析目的の収集は著作権法 47 条の 5 の範囲。
  設計上の担保は §Web 収集サービスの作法（robots 遵守・レート制限・本文丸写し排除・事実抽出のみ・出典明示・
  非公開情報対象外）として実装要件化し、S-3 で丸写し混入を機械検査する。個別サイトの利用規約で収集が明示
  禁止されるケースは運用ガイド（docs/07）の除外リスト運用で対応する。
- **`shared.tools.web_search` は placeholder（実機確認 2026-07-02）**: FW 提供の実収集機能は無いため
  テンプレ内自前実装とした（上記 Design Decision）。safe_fetch の標準機能化は将来の改善候補として
  別途検討する。
- **勝率推定の統計的妥当性**: 件数カウントによる勝率はサンプル数が薄いと不安定。サンプル数明示・参考値表示・
  段階フォールバックの 3 点で過信を防ぐが、統計的検定（信頼区間等）までは初版スコープ外（レポートに
  「参考指標」である旨を常時明記）。
- **収集情報の鮮度・正確性**: Web 公開情報は誤り・陳腐化があり得る。出典 URL・取得日時を必ず添え、
  人間（営業担当）のレビュー前提の下書きとして位置づける（レポート冒頭に免責を自動挿入）。
