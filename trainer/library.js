// 원본 177개를 모두 참고 가능. 의미 미확정 차트는 퀴즈에 섞지 않는다.
let libraryHand = "AA", libraryReturnFocus = null;
function libraryOptions(select, options) {
  select.replaceChildren(...options.map(([value, label]) => {
    const option = document.createElement("option");
    option.value = value; option.textContent = label;
    return option;
  }));
}
function librarySpot() { return DB.byId[$("library-spot").value]; }
function searchLibrary() {
  const query = $("library-search").value.trim().toLowerCase();
  const current = $("library-spot").value;
  const matches = DB.spots.filter((s) => `${s.title} ${s.id} ${s.profile || ""}`.toLowerCase().includes(query));
  libraryOptions($("library-spot"), matches.map((s) => [s.id, s.title]));
  if (matches.some((s) => s.id === current)) $("library-spot").value = current;
  configureLibrary();
}
function configureLibrary() {
  const spot = librarySpot();
  if (!spot) {
    $("library-grid").replaceChildren(); $("library-detail").replaceChildren();
    $("library-note").textContent = "검색 결과 없음";
    libraryOptions($("library-stack"), []); libraryOptions($("library-size"), []);
    return;
  }
  const stacks = Core.sampleStacks(spot);
  libraryOptions($("library-stack"), stacks.map((n) => [n ?? "", n == null ? "원본 미표기" : `${n}bb`]));
  $("library-stack").disabled = !spot.stack;
  const sizes = spot.open_size != null ? [spot.open_size] : spot.kind === "vs_open" && spot.villain_range_pct == null ? [2, 2.2, 2.3, 2.7, 3] : [null];
  libraryOptions($("library-size"), sizes.map((n) => [n ?? "", n == null ? "원본 미표기 / 해당 없음" : `${n}bb`]));
  $("library-size").disabled = sizes.length === 1;
  const stage = Core.STAGES.find((st) => st.match(spot, "all"));
  $("library-note").textContent = [stage ? `퀴즈: ${stage.name}` : "참고 전용 — 상황 의미 확인 전까지 출제 제외",
    spot.profile, spot.review, !spot.stack ? "스택 미표기: 특정 스택의 정답으로 단정하지 않아요." : ""].filter(Boolean).join(" · ");
  renderLibrary();
}
function renderLibrary() {
  const spot = librarySpot();
  if (!spot) return;
  const stack = $("library-stack").value === "" ? null : Number($("library-stack").value);
  const size = $("library-size").value === "" ? null : Number($("library-size").value);
  $("library-grid").replaceChildren(...Ranges.HANDS.map((hand) => {
    const cell = document.createElement("button");
    const rule = Ranges.resolve(spot, hand, { stack, size });
    const actions = Ranges.bestActions(rule);
    cell.className = (rule ? actions.length > 1 ? "mix" : actions[0] : "unknown") + (hand === libraryHand ? " me" : "");
    cell.textContent = hand;
    cell.title = `${hand}: ${rule ? Core.actText(rule.act, "first", spot.kind) : "현재 조건에 기록 없음"}`;
    cell.setAttribute("aria-label", cell.title);
    cell.style.background = actions.length > 1
      ? `repeating-linear-gradient(45deg, ${MINI_COLORS[actions[0]]}, ${MINI_COLORS[actions[0]]} 3px, ${MINI_COLORS[actions[1]]} 3px, ${MINI_COLORS[actions[1]]} 6px)`
      : MINI_COLORS[actions[0]] || "#2a3a30";
    cell.onclick = () => { libraryHand = hand; renderLibrary(); };
    return cell;
  }));
  const rule = Ranges.resolve(spot, libraryHand, { stack, size });
  const ex = Core.explain(spot, libraryHand);
  const heading = document.createElement("p");
  heading.textContent = `${libraryHand}: ${rule ? Core.actText(rule.act, "first", spot.kind) : "현재 조건에 기록 없음"}${rule?.src === "inferred" ? " · 맥락 추론" : ""} — 원본: ${ex.label}`;
  const lines = ex.lines.concat(rule?.note ? [rule.note] : []);
  const list = document.createElement("ul");
  lines.forEach((line) => { const item = document.createElement("li"); item.textContent = line; list.appendChild(item); });
  $("library-detail").replaceChildren(heading, list);
}
function openLibrary() {
  libraryReturnFocus = document.activeElement;
  $("library-search").value = "";
  searchLibrary();
  if (q && $("home").classList.contains("hidden")) {
    $("library-spot").value = q.spot.id; configureLibrary();
    for (const [id, value] of [["library-stack", q.stack], ["library-size", q.facing]]) {
      if ([...$(id).options].some((o) => o.value === String(value ?? ""))) $(id).value = value ?? "";
    }
    libraryHand = q.hand;
    renderLibrary();
  }
  $("library").classList.remove("hidden");
  $("library-search").focus();
}
function closeLibrary() { $("library").classList.add("hidden"); libraryReturnFocus?.focus(); }
$("library-open").onclick = openLibrary;
$("play-library-open").onclick = openLibrary;
$("library-close").onclick = closeLibrary;
$("library-search").oninput = searchLibrary;
$("library-spot").onchange = configureLibrary;
$("library-stack").onchange = renderLibrary;
$("library-size").onchange = renderLibrary;
// 각 선택 항목 위에서 휠을 돌리면 해당 항목만 한 칸씩 변경한다.
function bindLibraryWheel(id) {
  const select = $(id);
  let pending = 0, lastWheel = 0;
  select.closest("label").addEventListener("wheel", (e) => {
    if (e.ctrlKey || e.metaKey || Math.abs(e.deltaX) > Math.abs(e.deltaY) || !e.deltaY) return;
    if (select.disabled || select.options.length < 2) return;
    e.preventDefault();
    const delta = e.deltaY * (e.deltaMode === 1 ? 16 : e.deltaMode === 2 ? innerHeight : 1);
    const now = performance.now();
    if (now - lastWheel > 180 || Math.sign(pending) !== Math.sign(delta)) pending = 0;
    lastWheel = now;
    pending += delta;
    if (Math.abs(pending) < 40) return; // 트랙패드의 작은 움직임은 모아서 처리
    const next = Math.max(0, Math.min(select.options.length - 1, select.selectedIndex + Math.sign(pending)));
    pending = 0;
    if (next === select.selectedIndex) return;
    select.selectedIndex = next;
    select.dispatchEvent(new Event("change", { bubbles: true }));
  }, { passive: false });
}
bindLibraryWheel("library-spot");
bindLibraryWheel("library-stack");

document.addEventListener("keydown", (e) => {
  if ($("library").classList.contains("hidden")) return;
  if (e.key === "Escape") { e.preventDefault(); closeLibrary(); }
  if (e.key === "Tab") {
    const controls = [...$("library").querySelectorAll("button, input, select")].filter((el) => !el.disabled);
    if (e.shiftKey && document.activeElement === controls[0]) { e.preventDefault(); controls.at(-1).focus(); }
    else if (!e.shiftKey && document.activeElement === controls.at(-1)) { e.preventDefault(); controls[0].focus(); }
  }
});
