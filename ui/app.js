const messages = document.getElementById('messages');
const form = document.getElementById('form');
const input = document.getElementById('input');

marked.setOptions({ breaks: true });

form.addEventListener('submit', (e) => {
  e.preventDefault();
  const text = input.value.trim();
  if (!text) return;
  addMessage('user', text);
  input.value = '';
  sendMessage(text);
});

input.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    form.requestSubmit();
  }
});

async function sendMessage(text) {
  const bubble = addMessage('assistant', '');
  const loader = addLoader(bubble);

  const res = await fetch('/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message: text, backend: 'auto' }),
  });

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = '';
  let raw = '';
  let eventName = null;

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;

    buf += decoder.decode(value, { stream: true });
    const lines = buf.split('\n');
    buf = lines.pop();

    for (const line of lines) {
      if (line.startsWith('event:')) {
        eventName = line.slice(6).trim();
      } else if (line.startsWith('data:')) {
        const data = line.slice(5);

        if (eventName === 'stats') {
          try {
            const stats = JSON.parse(data);
            loader.remove();
            addStats(bubble, stats);
          } catch {}
          eventName = null;
        } else if (eventName === 'done' || eventName === 'error') {
          eventName = null;
        } else {
          if (loader.parentNode) loader.remove();
          raw += data;
          bubble.innerHTML = marked.parse(raw);
          messages.scrollTop = messages.scrollHeight;
        }
      } else if (line === '') {
        eventName = null;
      }
    }
  }
}

function addLoader(bubble) {
  const el = document.createElement('span');
  el.className = 'loader';
  el.innerHTML = '<span></span><span></span><span></span>';
  bubble.appendChild(el);
  return el;
}

function addStats(bubble, stats) {
  const el = document.createElement('div');
  el.className = 'stats';
  const inp = (stats.input_tokens || 0) + (stats.cache_read_tokens || 0) + (stats.cache_write_tokens || 0);
  const out = stats.output_tokens || 0;
  const cost = stats.cost_usd != null ? `$${stats.cost_usd.toFixed(4)}` : '';
  const cache = stats.cache_read_tokens ? ` · ${fmtNum(stats.cache_read_tokens)} cached` : '';
  const dur = stats.duration_ms ? ` · ${(stats.duration_ms / 1000).toFixed(1)}s` : '';
  el.textContent = `↑ ${fmtNum(inp)}${cache}  ↓ ${fmtNum(out)} tokens${cost ? '  ·  ' + cost : ''}${dur}`;
  bubble.after(el);
}

function fmtNum(n) {
  return n >= 1000 ? (n / 1000).toFixed(1) + 'k' : String(n);
}

function addMessage(role, content) {
  const div = document.createElement('div');
  div.className = `msg ${role}`;
  if (content) {
    if (role === 'assistant') div.innerHTML = marked.parse(content);
    else div.textContent = content;
  }
  messages.appendChild(div);
  messages.scrollTop = messages.scrollHeight;
  return div;
}
