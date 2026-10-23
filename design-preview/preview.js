// Offline design fixture. No WebSocket, fetch, persistent scores, or game commands.
const $ = (id) => document.getElementById(id);
let currentScreen = 'game';
const players = [
  { name: '나', chips: '9,800', pos: 'BTN', cards: ['As', 'Jd'], bet: '', me: true },
  { name: '민준', chips: '10,450', pos: 'SB', bet: '', folded: true },
  { name: '도윤', chips: '9,250', pos: 'BB', bet: '', action: '체크' },
  { name: '서연', chips: '10,100', pos: 'UTG', bet: '', folded: true },
  { name: '지우', chips: '8,900', pos: 'HJ', bet: '300', action: '베팅' },
  { name: '현우', chips: '9,500', pos: 'CO', bet: '', folded: true },
];
function card(code, small = false) {
  const el = document.createElement('div');
  el.className = 'card' + (small ? ' small' : '');
  if (code === 'back') { el.classList.add('back'); el.setAttribute('aria-label', '비공개 카드'); return el; }
  const suits = { s: '♠', h: '♥', d: '♦', c: '♣' };
  const suit = suits[code.slice(-1)], rank = code.slice(0, -1);
  if (/[hd]$/.test(code)) el.classList.add('red');
  el.setAttribute('aria-label', rank + suit);
  el.innerHTML = `<span class="pip">${suit}</span><span class="corner-top"><span class="r">${rank}</span><span class="s">${suit}</span></span><span class="corner-bottom"><span class="r">${rank}</span><span class="s">${suit}</span></span>`;
  return el;
}
const row = document.createElement('div'); row.className = 'board-row';
['Js', '7h', '2c'].forEach(code => row.append(card(code)));
$('community').append(row);
function renderSeats() {
  $('seats').replaceChildren();
  const mobile = innerWidth <= 600;
  const proposal = document.body.classList.contains('proposed');
  players.forEach((p, i) => {
    const angle = i / players.length * 2 * Math.PI;
    const rx = mobile ? (proposal ? 43 : 44) : (proposal ? 48 : 52);
    const ry = mobile ? (proposal ? 43 : 44) : (proposal ? 44 : 47);
    const seat = document.createElement('div');
    seat.className = 'seat' + (p.me ? ' me active' : '') + (p.folded ? ' folded' : '');
    seat.style.left = `${50 - rx * Math.sin(angle)}%`;
    seat.style.top = `${50 + ry * Math.cos(angle)}%`;
    const cards = document.createElement('div'); cards.className = 'player-cards';
    (p.cards || ['back', 'back']).forEach(c => cards.append(card(c, true)));
    seat.append(cards);
    const nameplate = document.createElement('div'); nameplate.className = 'nameplate';
    nameplate.innerHTML = `<div class="pname"><span class="pos-chip">${p.pos}</span>${p.name}${p.me ? '<span class="dealer-btn">D</span>' : ''}</div><div class="pchips">${p.chips}</div>`;
    seat.append(nameplate);
    const bet = document.createElement('div'); bet.className = 'pbet'; bet.textContent = p.bet; seat.append(bet);
    const action = document.createElement('div'); action.className = 'paction'; action.textContent = p.folded ? '폴드' : (p.action || ''); seat.append(action);
    $('seats').append(seat);
  });
}
function screen(name) {
  currentScreen = name;
  $('game-screen').classList.toggle('hidden', name !== 'game');
  $('join-screen').classList.toggle('hidden', name !== 'join');
  document.querySelectorAll('.preview-tools [data-screen]').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.screen === name)));
  document.body.classList.remove('show-log', 'show-chat');
}
document.querySelectorAll('[data-theme]').forEach(button => button.addEventListener('click', () => {
  document.body.classList.toggle('proposed', button.dataset.theme === 'proposed');
  document.querySelectorAll('[data-theme]').forEach(b => b.setAttribute('aria-pressed', String(b === button)));
  renderSeats();
}));
document.querySelectorAll('[data-screen]').forEach(button => button.addEventListener('click', () => screen(button.dataset.screen)));
document.querySelectorAll('[data-amount]').forEach(button => button.addEventListener('click', () => {
  $('raise-amount').value = $('raise-slider').value = button.dataset.amount;
}));
$('raise-slider').addEventListener('input', () => { $('raise-amount').value = $('raise-slider').value; });
$('raise-amount').addEventListener('input', () => { $('raise-slider').value = $('raise-amount').value; });
let feedbackTimeout;
function feedback(text) {
  $('feedback').textContent = text; $('feedback').classList.add('visible');
  clearTimeout(feedbackTimeout); feedbackTimeout = setTimeout(() => $('feedback').classList.remove('visible'), 2800);
}
document.querySelectorAll('[data-action]').forEach(button => button.addEventListener('click', () => {
  feedback(`${button.dataset.action}${button.dataset.action === '레이즈' ? ' ' + $('raise-amount').value : ''} 선택 · 디자인 미리보기입니다`);
}));
document.querySelectorAll('[data-dialog]').forEach(button => button.addEventListener('click', () => {
  $('dialog-title').textContent = button.dataset.dialog;
  $('dialog-copy').textContent = '이번 시안은 입장 화면과 게임 테이블을 비교하는 미리보기입니다. 이 메뉴는 실제 서비스에 연결하지 않았어요.';
  $('preview-dialog').showModal();
}));
document.querySelectorAll('[data-panel]').forEach(button => button.addEventListener('click', () => {
  const key = 'show-' + button.dataset.panel;
  const opened = document.body.classList.contains(key);
  document.body.classList.remove('show-log', 'show-chat');
  document.body.classList.toggle(key, !opened);
}));
$('join-form').addEventListener('submit', event => {
  event.preventDefault();
  $('room-label').textContent = $('room-input').value.trim() || '금요일 홀덤';
  players[0].name = $('name-input').value.trim() || '나'; renderSeats(); screen('game');
});
$('chat-form').addEventListener('submit', event => {
  event.preventDefault();
  const value = $('chat-input').value.trim(); if (!value) return;
  const line = document.createElement('div'); const name = document.createElement('span'); name.className = 'cname'; name.textContent = players[0].name;
  line.append(name, document.createTextNode(' ' + value)); $('chat-messages').append(line); $('chat-input').value = '';
  line.scrollIntoView({ block: 'nearest' });
});
window.addEventListener('resize', renderSeats);
renderSeats(); screen(currentScreen);
