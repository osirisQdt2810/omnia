# Smart Notes — Đồ thị phụ thuộc giữa các field (Field Dependency Graph)

> Tài liệu này viết bằng tiếng Việt theo yêu cầu. Mã nguồn, docstring và comment trong code
> vẫn bằng tiếng Anh theo quy ước dự án.

Tài liệu mô tả **cách tính năng đồ thị phụ thuộc đang được triển khai**, **cách nó hoạt động khi
sinh field**, và **các kịch bản (scenario) thường gặp**. Dành cho người maintain Omnia.

---

## 1. Vấn đề & mục tiêu

Trong Smart Notes, mỗi note type có một **base field** (đầu vào, ví dụ `Word`) và nhiều field
được sinh tự động bằng LLM/TTS (định nghĩa, ví dụ, audio, ảnh…). Nhiều field **chỉ sinh được khi
field khác đã có**:

- `Definition` cần `Word`.
- `Example 1 (audio)` cần `Example 1` (phải có câu ví dụ mới đọc thành tiếng được).
- `Meaning (vi)` cần `Definition` hoặc `Word`…

Trước đây quan hệ phụ thuộc này **ngầm định**: nó được suy ra từ các placeholder `{{Field}}` trong
prompt và engine tự sắp thứ tự sinh. Nhưng nó **vô hình, không sửa được, và không chặn**: nếu một
tiền đề rỗng, field phụ thuộc vẫn sinh ra (thường ra rác).

**Mục tiêu của tính năng:**

1. Biến đồ thị phụ thuộc thành **tường minh** (lưu trong config), **trực quan** (vẽ ra), **sửa được**.
2. Thêm phân loại cạnh **hard (đỏ)** và **soft (xanh)** với ngữ nghĩa khác nhau.
3. **Chặn** việc sinh một field khi tiền đề **bắt buộc (hard)** của nó chưa có.
4. Cho **Auto-prompt** đề xuất luôn đồ thị (khi đang trống) cùng với prompt.

---

## 2. Khái niệm cốt lõi

### 2.1. Node và Edge

- **Node** = một field của note type (kể cả base field). Base field là đầu vào, không bao giờ bị sinh.
- **Edge `A → B`** = "A là **tiền đề** của B" (muốn sinh B thì cần A). Chiều mũi tên: từ tiền đề sang
  field phụ thuộc.

### 2.2. Hai loại cạnh

| Loại | Màu | Ngữ nghĩa | Ảnh hưởng thứ tự sinh | Có chặn không? |
|------|-----|-----------|-----------------------|----------------|
| **hard** | 🔴 đỏ | B **bắt buộc** phải có A | Có (A sinh trước B) | **Có** — A rỗng/bị chặn ⇒ B bị bỏ qua |
| **soft** | 🟢 xanh | A là **ngữ cảnh tùy chọn** cho B | Có (A sinh trước B) | Không — B vẫn sinh kể cả khi A rỗng |

Ngoài ra mỗi cạnh có cờ **`derived`**:

- `derived = true`: cạnh **suy ra** từ một `{{A}}` trong prompt của B (vẽ bằng nét đứt).
- `derived = false`: cạnh **người dùng tự thêm** (explicit), không có trong prompt.

### 2.3. Nguồn sự thật lai (hybrid source of truth)

Đồ thị hiệu dụng = **HỢP** của hai nguồn:

1. **Cạnh suy ra**: với mỗi `{{A}}` trong prompt của B (và `source_field` của rule TTS) → cạnh
   `A → B`, mặc định **hard**.
2. **Cạnh tường minh**: danh sách `depends_on` lưu trên từng field.

Quy tắc hợp nhất:

- Nếu một cặp `(A, B)` xuất hiện ở **cả hai** nguồn → entry tường minh **đè** loại (kind) của cạnh
  suy ra (ví dụ: prompt có `{{Word}}` (suy ra, hard) nhưng người dùng đặt `depends_on` Word = soft
  ⇒ cạnh thành **soft**).
- Cạnh chỉ có ở `depends_on` (không có `{{ref}}`) → thêm vào với `derived = false`.
- Khớp tên field **không phân biệt hoa/thường** (tên field do người dùng đặt).
- Cạnh trỏ tới field **không tồn tại** (đã đổi tên/xóa) → **bỏ qua** khi dựng đồ thị.

> Hệ quả quan trọng: **config cũ không cần migrate**. Nếu chưa ai vẽ gì, đồ thị vẫn tự suy ra đầy đủ
> từ các `{{ref}}` sẵn có trong prompt.

---

## 3. Mô hình dữ liệu

File: [`src/omnia/plugins/smart_notes/config.py`](../src/omnia/plugins/smart_notes/config.py)

```python
class FieldDep(_Strict):
    field: str          # tên field tiền đề
    kind: str = "hard"  # "hard" | "soft"

class SmartNotesFieldConfig(_Strict):
    field: str
    ...
    depends_on: list[FieldDep] = []   # các cạnh tường minh TRỎ VÀO field này

class SmartNotesFieldRule(_Strict):   # bản "đã biên dịch" mà engine tiêu thụ
    ...
    depends_on: list[FieldDep] = []
```

**Lưu ý thiết kế:** ở tầng model **không** kiểm tra self-reference / chu trình / field lạ. Lý do:
một field có thể hợp lệ khi tham chiếu tới field **chưa tạo**; việc kiểm tra toàn cục (cần nhìn cả
note type cùng lúc) thuộc về **tầng engine**.

**Lưu trữ:** `depends_on` nằm trong blob config của smart_notes trong **collection DB** (đồng bộ qua
AnkiWeb), không phải file TOML. Không thêm namespace mới.

---

## 4. Kiến trúc & các thành phần

```
                    ┌───────────────────────────── PURE (không import aqt/anki) ─────────────────────────────┐
prompt {{refs}} ─┐  │  rules.rule_prerequisites(rule)  ──►  "field này phụ thuộc gì" (nguồn sự thật DUY NHẤT) │
                 ├──┤        │                                                                                │
depends_on     ──┘  │        ├─► ordering.order_rules()      → sắp thứ tự topo (cả 2 loại đều sắp)            │
                    │        ├─► service.generate_note()     → chặn theo cạnh HARD + báo BlockedField          │
                    │        └─► graph.build_field_graph()   → FieldGraph (nodes+edges, +cờ derived)           │
                    │                    │                                                                     │
                    │                    └─► graph.layered_layout()  → gán column/row (longest-path)           │
                    └─────────────────────────────────────────────────────────────────────────────────────────┘
                                         │ (nodes+edges+layout)
                    ┌──────────────────── GUI (glue) ──────────────────────────────────────────────────────────┐
                    │ html.graph_payload() → JSON  ──►  web/06-graph.js (vẽ SVG, kéo/click/Delete)               │
                    │ dialog._on_graph_recompute (op pycmd "graph_recompute")  ◄── mỗi lần sửa cạnh              │
                    │ Save (op cũ) ── collectRows() đọc depends_on ── lưu vào collection DB                       │
                    └─────────────────────────────────────────────────────────────────────────────────────────┘
```

### 4.1. `rule_prerequisites` — nguồn sự thật duy nhất

File: [`engine/rules.py`](../src/omnia/plugins/smart_notes/engine/rules.py)

```python
def rule_prerequisites(rule) -> list[tuple[str, str]]:
    """Trả về (tên_field_tiền_đề, kind_hiệu_dụng): cạnh suy ra từ {{ref}}/source (mặc định
    'hard') HỢP với depends_on, trong đó kind tường minh đè kind suy ra."""
```

Cả ba nơi dùng chung hàm này nên **ngữ nghĩa hard/soft chỉ định nghĩa một chỗ**, không thể lệch:

- `ordering.order_rules` — dùng (bỏ qua kind, **cả hai loại** đều ảnh hưởng thứ tự).
- `service` — lọc `kind == "hard"` để biết tiền đề bắt buộc.
- `graph.build_field_graph` — dựng cạnh, gắn thêm cờ `derived`.

### 4.2. `graph.py` — đồ thị thuần + bố cục

File: [`engine/graph.py`](../src/omnia/plugins/smart_notes/engine/graph.py)

- `GraphEdge{src, dst, kind, derived}`, `FieldNode{name, is_base, generatable, column, row}`,
  `FieldGraph{nodes, edges}`.
- `build_field_graph(config) -> FieldGraph`: dựng node (base + mọi field) và edge (hợp suy ra ∪
  tường minh, đè kind, bỏ cạnh trỏ field không tồn tại).
- `validate_acyclic(graph)`: ném `SmartNotesCycleError` nếu có chu trình/tự trỏ (xét cả hai loại).
- `would_create_cycle(graph, src, dst)`: kiểm tra **trước** khi thêm cạnh (bản mirror cho phía client).
- `layered_layout(graph) -> FieldGraph`: gán `column` = độ sâu **longest-path** từ node gốc, `row` =
  thứ tự ổn định trong cột. **Tất toán deterministic** — đây là **nguồn sự thật về vị trí**; JS chỉ
  đặt node theo `(column, row)`, **không** tự cài longest-path.

### 4.3. Sinh field + chặn

File: [`engine/service.py`](../src/omnia/plugins/smart_notes/engine/service.py)

```python
def generate_note(config, fields, *, allow_empty_fields=False, force_overwrite=False)
    -> tuple[list[(rule, GenerationResult)], list[BlockedField]]
```

- Sắp các rule theo `order_rules` (thứ tự topo) rồi sinh lần lượt; kết quả **text** được "chained"
  vào field map để rule sau interpolate được giá trị mới.
- Trước khi sinh mỗi rule, kiểm tra **tiền đề hard**: nếu một tiền đề hard **rỗng và chưa được sinh**
  → bỏ qua field đó, ghi vào `BlockedField{target_field, missing}`, **không** đặt giá trị vào map
  ⇒ các field hard phụ thuộc tiếp theo bị **chặn lan truyền**.
- **Tiền đề được tính là "đã có"** nếu: có giá trị non-blank trong map, **hoặc** đã sinh thành công
  trong lần chạy này (`produced` — kể cả ảnh/tts vốn không ghép vào map text), hoặc đã sẵn có non-blank
  từ đầu. ⇒ Ảnh/tts làm tiền đề **không bị chặn nhầm**.
- `BlockedField` được `integration/batch.py` đếm và hiển thị ("K blocked — missing prerequisites").

---

## 5. UI — tab Dependencies

Files: [`web/06-graph.js`](../src/omnia/gui/smart_notes/web/06-graph.js),
[`web/page.html`](../src/omnia/gui/smart_notes/web/page.html),
[`web/page.css`](../src/omnia/gui/smart_notes/web/page.css),
[`gui/smart_notes/html.py`](../src/omnia/gui/smart_notes/html.py),
[`gui/smart_notes/dialog.py`](../src/omnia/gui/smart_notes/dialog.py)

- Một công tắc **Fields ⇄ Dependencies** ngay trên bảng field. Tab Dependencies chứa một `<svg>` thuần
  (vanilla, **không thư viện ngoài** vì CSP của webview chặn CDN).
- **Tương tác:**
  - **Kéo** từ node A sang node B → thêm cạnh `A → B` mặc định **hard**. Trước khi thêm, client tự
    kiểm tra chu trình (`wouldCreateCycle`, BFS) — nếu tạo vòng thì **từ chối + toast**.
  - **Click** một cạnh → đổi **hard ↔ soft** (và chọn cạnh đó).
  - **Chọn cạnh + Delete/Backspace** → xóa.
- **Luồng dữ liệu (layout luôn ở Python):** mỗi lần sửa cạnh, JS cập nhật `data-depends-on` của row
  đích rồi gọi op **`graph_recompute`** (gửi `note_type` + `base_field` + `rows` hiện tại). Python
  dựng lại đồ thị + layout và trả về `{graph}`; JS chỉ vẽ. Nếu Python phát hiện chu trình (chốt chặn
  phía server) → trả `{error}` → toast, không crash.
- **Lưu:** sửa cạnh chỉ đổi `depends_on` trong các row trên trang. Bấm **Save** dùng lại op lưu cũ:
  `collectRows()` đọc `depends_on` (qua `readDependsOn`) → `note_type_config_from_payload` → lưu vào
  collection DB. **Không có đường lưu riêng cho đồ thị.**
- **Vị trí node là tự tính** (không lưu tọa độ kéo-thả thủ công). Người dùng kéo để **tạo cạnh**, còn
  bố cục do Python quyết định để luôn gọn gàng.

---

## 6. Auto-prompt sinh kèm đồ thị

File: [`authoring/author.py`](../src/omnia/plugins/smart_notes/authoring/author.py),
[`authoring/models.py`](../src/omnia/plugins/smart_notes/authoring/models.py)

- `build_auto_smart_prompt(...)` yêu cầu LLM trả về cho mỗi field: `{"type", "prompt",
  "depends_on": [{"field", "kind"}]}` với luật "hard = nội dung bắt buộc, soft = ngữ cảnh tùy chọn,
  chỉ tham chiếu field tồn tại, **không tạo chu trình**".
- Nếu đồ thị **đã có cạnh**, các cạnh hiện tại được **serialize vào prompt** kèm chỉ thị **GIỮ
  NGUYÊN** chúng và chỉ thêm cạnh còn thiếu (qua `existing_deps`).
- `parse_auto_smart_response` đọc `depends_on` một cách **khoan dung**: thiếu `kind` → mặc định hard;
  giá trị lạ → hard; entry thiếu `field` hoặc tự trỏ → **bỏ**. Không kiểm tra chu trình ở đây (engine lo).
- `apply_auto_smart` chỉ đặt `depends_on` cho field **chưa có cạnh tường minh nào** (điền chỗ trống,
  **không đè** cạnh người dùng đã vẽ); type/prompt giữ hành vi cũ.
- Sau khi Auto-prompt, nếu tab Dependencies đang mở thì `web/05-handlers.js` gọi `refreshGraphIfOpen()`
  để vẽ lại.

---

## 7. Các kịch bản (scenarios)

Giả sử note type vocab: base = `Word`; các field `Definition`, `Meaning (vi)`, `Example 1`,
`Example 1 (audio)`, `Example 1 (vi)`, `Synonyms`.

### S1 — Chuỗi tuyến tính (cơ bản)
`Definition.prompt = "Define {{Word}}"`, `Meaning (vi).prompt = "Dịch {{Definition}} sang tiếng Việt"`.
→ Đồ thị suy ra: `Word →(hard) Definition →(hard) Meaning (vi)`. Thứ tự sinh: Word có sẵn → Definition
→ Meaning (vi). Mọi cạnh nét đứt (derived).

### S2 — Tiền đề hard rỗng ⇒ chặn lan truyền
`Word` rỗng ở một note (hiếm, nhưng có). `Definition` hard-depends `Word` → `Definition` bị **bỏ qua**
(`BlockedField(Definition, missing=[Word])`). `Meaning (vi)` hard-depends `Definition` → cũng bị chặn
theo. Batch báo "2 blocked".

### S3 — Audio phụ thuộc câu ví dụ
`Example 1 (audio)` là field TTS có `source_field = Example 1`. → cạnh suy ra `Example 1 →(hard)
Example 1 (audio)`. Nếu `Example 1` sinh ra rỗng → audio bị chặn. Nếu `Example 1` sinh **thành công**
(có chữ) → audio sinh bình thường. Quan trọng: dù audio không "chained" vào map text, nó vẫn được tính
là "đã sản xuất" nên field nào hard-depend vào audio cũng không bị chặn nhầm (xem mục 4.3).

### S4 — Đổi cạnh hard thành soft (override)
Prompt của `Meaning (vi)` có `{{Definition}}` (suy ra hard). Người dùng thấy "dịch nghĩa vẫn ổn dù
chưa có definition" → vào tab Dependencies, **click** cạnh `Definition → Meaning (vi)` để chuyển
**soft**. Hệ thống ghi `depends_on=[{field:"Definition", kind:"soft"}]` lên row `Meaning (vi)` (đè kind
suy ra). Từ đó: vẫn sinh Definition trước (vì soft cũng sắp thứ tự), nhưng nếu Definition rỗng thì
`Meaning (vi)` **vẫn sinh** (không chặn).

### S5 — Thêm cạnh soft không có trong prompt
`Synonyms` muốn "tham khảo" `Definition` nhưng prompt không nhắc `{{Definition}}`. Người dùng **kéo**
`Definition → Synonyms` rồi click cho thành **xanh (soft)**. → `depends_on` của `Synonyms` thêm entry
soft (`derived=false`). Tác dụng: Definition sinh trước Synonyms; không chặn.

### S6 — Cố tạo chu trình
`A → B` đã tồn tại, người dùng kéo `B → A`. Client `wouldCreateCycle` phát hiện B có thể tới A → **từ
chối**, hiện toast "Would create a cycle", không thêm. (Server cũng có chốt chặn: `graph_recompute`
trả `{error}` nếu vì lý do nào đó vẫn lọt.)

### S7 — Cạnh trỏ vào base field
Người dùng kéo `Definition → Word`. Vì `Word` là base (đầu vào, không có row để sinh) → toast "The base
field is the input — it can't depend on another field", không thêm.

### S8 — Xóa một cạnh suy ra từ prompt
Người dùng chọn cạnh `Word → Definition` (nét đứt) và bấm Delete. Hệ thống xóa entry `depends_on` (nếu
có) nhưng cạnh **vẫn còn** vì prompt vẫn chứa `{{Word}}` → sau recompute cạnh hiện lại; toast nhắc "edit
the prompt to remove it". ⇒ Muốn bỏ hẳn cạnh suy ra thì phải sửa prompt.

### S9 — Auto-prompt trên đồ thị trống
Field chưa cấu hình gì, đồ thị trống. Bấm ✨ Auto-prompt → LLM trả về type + prompt + `depends_on` cho
từng field; vì các field đều "trống cạnh" nên `apply_auto_smart` ghi cả `depends_on`. Mở tab
Dependencies thấy đồ thị đã được đề xuất sẵn.

### S10 — Auto-prompt khi đã có cạnh do người dùng vẽ
Người dùng đã vẽ vài cạnh. Bấm Auto-prompt → prompt gửi kèm danh sách cạnh hiện có + chỉ thị GIỮ NGUYÊN.
`apply_auto_smart` chỉ thêm `depends_on` cho field **chưa có cạnh**; field người dùng đã vẽ **không bị
đè**.

### S11 — Đổi tên / xóa field còn được tham chiếu
Một `depends_on` trỏ tới field đã đổi tên. Khi dựng đồ thị, cạnh "treo" đó bị **bỏ qua** (không vẽ).
Dữ liệu `depends_on` không bị xóa khỏi storage ngay (đổi tên về lại sẽ khôi phục); lần Save kế tiếp qua
dialog mới dọn các entry không còn hợp lệ.

### S12 — Tương tác với `overwrite`
Một tiền đề hard "đã có sẵn nội dung và không bị overwrite" vẫn được tính là **non-blank** → **không
chặn**. Khi bật `force_overwrite`, các tiền đề được sinh lại trước (nhờ thứ tự topo); nếu sinh lại ra
rỗng thì các field hard phụ thuộc bị chặn.

---

## 8. Bản đồ file (nơi tìm từng phần)

| Thành phần | File |
|------------|------|
| Model `FieldDep` / `depends_on` | `plugins/smart_notes/config.py` |
| Nguồn sự thật tiền đề | `plugins/smart_notes/engine/rules.py` (`rule_prerequisites`) |
| Đồ thị thuần + layout | `plugins/smart_notes/engine/graph.py` |
| Thứ tự sinh | `plugins/smart_notes/engine/ordering.py` (`order_rules`) |
| Chặn theo hard + `BlockedField` | `plugins/smart_notes/engine/service.py` (`generate_note`) |
| Đếm/báo blocked | `plugins/smart_notes/integration/batch.py`, `review.py` |
| Auto-prompt sinh deps | `plugins/smart_notes/authoring/{models,author}.py` |
| Payload đồ thị cho trang | `gui/smart_notes/html.py` (`graph_payload`) |
| Op `graph_recompute` | `gui/smart_notes/dialog.py` |
| Trình vẽ SVG | `gui/smart_notes/web/06-graph.js` |
| Markup/CSS tab | `gui/smart_notes/web/page.html`, `page.css` |

---

## 9. Cách kiểm thử

```bash
# Test logic (Anki bị stub):
.venv/bin/python -m pytest tests/ -q -m "not llm and not tts"

# Cú pháp bundle webview:
cat src/omnia/gui/smart_notes/web/0*.js | node --check -
```

Test liên quan: `tests/plugins/test_smart_notes_graph.py` (dựng/validate/cycle/layout),
`tests/plugins/test_smart_notes.py` (model `FieldDep`, ordering với cạnh tường minh, chặn,
auto-prompt parse/apply), `tests/gui/test_smart_notes_html.py` (`graph_payload`, round-trip
`depends_on`).

**Kiểm tra trực quan trong Anki** (không unit-test được): Tools → Omnia → Smart Notes → chọn note type
→ nút **Dependencies** → kéo/click/Delete cạnh → Save → mở lại (cạnh còn nguyên); ✨ Auto-prompt trên
đồ thị trống thấy đề xuất cạnh.

---

## 10. Quyết định thiết kế đáng nhớ

- **Layout tính ở Python**, JS chỉ vẽ → tránh hai nơi cài longest-path lệch nhau.
- **Một hàm `rule_prerequisites`** cho cả ordering/blocking/graph → ngữ nghĩa hard/soft không trôi.
- **Hybrid**: prompt `{{ref}}` vẫn là một nguồn cạnh (mặc định hard); `depends_on` để bổ sung/đè. Nhờ
  vậy config cũ tự có đồ thị, không cần migrate.
- **Không lưu tọa độ node**: chỉ lưu *quan hệ* (cạnh), còn vị trí luôn được tính lại → đồ thị luôn gọn.
- **Kiểm tra chu trình hai lớp**: client (chặn ngay khi kéo) + server (chốt chặn khi recompute).

---

# Phần II — Đồng bộ HAI CHIỀU prompt ⇄ graph (Feature 1 & 2) + UI mới

Phần I (mục 1–10) mô tả đồ thị *một chiều* (suy ra từ prompt). Phần II mô tả việc nâng cấp thành
**đồng bộ hai chiều** và giao diện vẽ lại đẹp hơn.

## 11. Lớp dùng chung: `engine/consistency.py`

Một lớp thuần (không LLM) là "xương sống" mà cả hai chiều dùng chung nên không bao giờ lệch nhau:

- `NodeEdgeSet.derive(target, prompt, depends_on, known_fields)` → tập cạnh-vào `(src, kind)` tại một
  node, từ một prompt *ứng viên* (dùng để kiểm tra trước khi áp).
- `FieldGraph.node_edge_set(target)` → tập cạnh-vào từ đồ thị đã dựng. Hai đường này **bắt buộc khớp
  nhau** (có test bất biến) — cùng đi qua `compile_field_rule` + `rule_prerequisites`.
- `NodeEdgeSet.diff(after)` → `ConsistencyResult{ok, added_fields, removed_fields, kind_changes,
  bad_syntax, messages}`. `ok` **bỏ qua** khác biệt *kind* (hard/soft) — kind được áp riêng theo cơ chế
  lockstep, không do guard rail quyết định.
- `validate_prompt_syntax` / `interpolation.validate_brace_syntax`: bắt cú pháp `{{}}` sai (cloze-aware).

## 12. Feature 1 — prompt → graph (phân loại hard/soft bằng LLM)

Code chỉ biết "có cạnh" (vì có `{{ref}}`), **không** biết cạnh đó *bắt buộc* (hard) hay *tùy chọn*
(soft) — đó là phán đoán ngữ nghĩa, nên cần **LLM (temperature 0)**.

- **Khi nào chạy**: sau khi Save một prompt (popup), sau Auto-prompt, sau Improve-all.
- **Cách chạy**: op `classify_deps` (off-thread); với note lớn dùng `classify_dependencies_batch`
  (gộp **một** lời gọi cho nhiều field). Kết quả ghi vào `depends_on` của field, đẩy về trang qua
  `window.__snDepsResult` **chỉ từ success-callback** (eval_js off-thread sẽ làm Anki/Qt segfault).
- **Tôn trọng người dùng (B2)**: `reconcile_field_deps` **chỉ phân loại ref MỚI**; ref đã có kind
  (người dùng đặt hoặc lần trước) giữ nguyên → không nhấp nháy. Kind do classifier ghi mang cờ
  `auto=True` (phân biệt với cạnh người dùng).
- **Bền qua recompute (B1)**: kind soft phải được ghi thành entry tường minh, nếu không recompute sẽ
  về hard mặc định.

## 13. Feature 2 — graph → prompt (sửa cạnh → viết lại prompt)

Khi người dùng sửa cạnh rồi bấm **↻ Sync prompts**:

1. **Diff** (client, `diffEdges`): so `depends_on` hiện tại với mốc `lastSyncedDeps` (chụp lúc load /
   save / sau Feature-1) → danh sách node đích thay đổi + loại đổi (add/remove/toggle).
2. **Hàng đợi theo thứ tự topo, xử lý từng node một (lazy)**: rewrite của node sau được tính **sau khi**
   node trước đã Apply (đọc trạng thái row hiện tại) → không bị "viết C dựa trên B cũ". Op
   `rewrite_edges` (off-thread) gọi `PromptAuthor.rewrite_for_edge_change` (có guard rail + 1 lần retry).
3. **Popover diff** (`#sn-diff-pop`): "Was" (prompt cũ, chỉ đọc) ↑ mũi tên ↓ "Now" (prompt mới, sửa
   được). Có nút **✨ Improve** (op `improve_prompt_pinned`, ghim đúng tập phụ thuộc).
4. **Guard rail (op `validate_prompt`, debounce 250ms)**: mọi prompt rời popover (LLM/sửa tay/Improve)
   phải suy ra **đúng** tập cạnh dự định tại node + cú pháp `{{}}` hợp lệ, nếu không **Apply bị khoá**.
   Chính là Feature 1 dùng ngược → dùng chung lớp `consistency`.
5. **Apply**: pre-check chu trình (client) **trước khi** ghi → rollback nếu tạo vòng; ghi prompt + set
   `depends_on` theo kind dự định **lockstep** (kind không trôi) → recompute (backstop
   `validate_acyclic`). Backstop cuối cùng: `_on_save` **từ chối lưu** nếu đồ thị có chu trình.

LLM (`authoring/`): `DEPENDENCY_CLASSIFIER_SYSTEM` (phân loại), `FLASHCARD_EXPERT_SYSTEM` (viết lại);
prompt được viết theo "reasoning shape" — không gắn cứng tên field của một note type nào → generic.

## 14. UI mới (mục 5 cũ được vẽ lại)

- **Layout**: `FieldGraph.flow_layout()` (Python) — xếp theo tầng, **wrap cột cao thành nhiều lane** và
  căn giữa → hết cảnh 33 field một cột. JS chỉ vẽ theo toạ độ Python trả về.
- **Canvas**: một `<g>` pan/zoom (kéo nền = pan, lăn chuột = zoom quanh con trỏ, fitView khi mở).
- **Cử chỉ**: kéo **thân node = di chuyển** (chỉ thị giác, không lưu); kéo **handle (cổng bên phải) =
  tạo cạnh**; click cạnh = đổi hard↔soft; chọn + Delete = xoá.
- **Thẩm mỹ**: cạnh Bézier gradient (hard đỏ / soft xanh), hit-twin để dễ click, node nền gradient mờ,
  animation CSS. **Không dùng SVG `<filter>` / `overflow:auto`** (gây blank trên QtWebEngine/macOS Metal).

## 15. Bản đồ file (phần mở rộng)

| Thành phần | File |
|------------|------|
| Lớp consistency dùng chung | `engine/consistency.py` |
| Phân loại + viết lại bằng LLM | `authoring/{author,models,persona}.py` |
| Op nối (classify/validate/rewrite/improve) + backstop lưu | `gui/smart_notes/dialog.py` |
| Diff cạnh client + hàng đợi + popover + pan/zoom/cử chỉ | `gui/smart_notes/web/06-graph.js` |
| Popover markup + style | `gui/smart_notes/web/page.html`, `page.css` |
| Bố cục đẹp (flow_layout) | `engine/graph.py` |
