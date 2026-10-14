let token = '';
let refreshing = false;
const statusLabels = {
  delivered: 'Waiting for acknowledgment', awaiting_ack: 'Waiting for acknowledgment',
  no_ack: 'No response yet', unconfirmed: 'Handling unconfirmed',
  in_progress: 'Being handled', stalled: 'No recent update', needs_input: 'Waiting for you',
  completed: 'Completed', failed: 'Failed'
};
const $ = id => document.getElementById(id);
async function api(path, data) {
  const response = await fetch(path, {method: data ? 'POST' : 'GET', headers: {
    Authorization: `Bearer ${token}`, 'Content-Type': 'application/json'},
    ...(data ? {body: JSON.stringify(data)} : {})});
  const result = await response.json();
  if (!response.ok) throw new Error(result.detail || 'Request failed');
  return result;
}
function element(tag, text, className) {
  const node = document.createElement(tag); node.textContent = text;
  if (className) node.className = className;
  return node;
}
async function refresh() {
  if (refreshing) return;
  refreshing = true;
  try {
    const data = await api('/api/requests');
    $('login').hidden = true; $('workspace').hidden = false;
    $('connection').textContent = `${data.dots_connected ? 'Dots connected' : 'Dots waiting for connection'} · ${data.router === 'rules' ? 'Command routing' : 'AI routing'}`;
    $('requests').replaceChildren(); $('empty').hidden = data.requests.length > 0;
    const attentionCount = data.requests.filter(row => row.attention || row.status === 'needs_review').length;
    $('attention').textContent = attentionCount ? `${attentionCount} request${attentionCount === 1 ? '' : 's'} need attention` : '';
    $('attention').hidden = !attentionCount;
    $('last-updated').textContent = `Updated ${new Date().toLocaleTimeString()} · Refreshes every 15 seconds`;
    for (const row of data.requests) {
      const article = document.createElement('article'), status = row.display_status || row.status;
      if (row.attention) article.className = 'needs-attention';
      article.append(element('span', statusLabels[status] || status.replaceAll('_', ' '), `badge ${row.attention ? 'warning' : ''}`),
        element('p', row.transcript), element('p', row.detail, 'detail'),
        element('small', `${row.destination || 'Unassigned'} · ${new Date(row.created * 1000).toLocaleString()}`));
      if (row.attention_detail) article.append(element('p', row.attention_detail, 'attention-detail'));
      if (row.route === 'dots') {
        const milestones = [['delivered_at', 'Received'], ['acknowledged_at', 'Acknowledged'], ['progress_at', 'Last update'], ['finished_at', 'Finished']]
          .filter(([key]) => row[key] != null)
          .map(([key, label]) => `${label}: ${new Date(row[key] * 1000).toLocaleString()}`);
        if (milestones.length) article.append(element('small', milestones.join(' · ')));
        article.append(element('small', `Task ID: ${row.id}`, 'task-id'));
      }
      if (row.status === 'needs_review') {
        const controls = element('div', '', 'row'), select = document.createElement('select');
        select.setAttribute('aria-label', 'Choose destination');
        for (const destination of data.destinations) {
          const option = element('option', `Note → ${destination}`); option.value = `note:${destination}`; select.append(option);
        }
        const dots = element('option', 'Task → Dots'); dots.value = 'dots:dots'; select.append(dots);
        const button = element('button', 'Approve & send');
        button.onclick = () => action(async () => {
          if (!confirm('If an earlier delivery may have succeeded, check its destination first. Send this request?')) return;
          const [route, destination] = select.value.split(':');
          await api(`/api/requests/${row.id}/resolve`, {route, destination}); await refresh();
        });
        controls.append(select, button); article.append(controls);
      }
      $('requests').append(article);
    }
  } finally { refreshing = false; }
}
async function action(fn) {
  $('message').textContent = '';
  try { await fn(); } catch (e) { $('message').textContent = e.message; }
}
$('connect-form').onsubmit = e => {e.preventDefault(); token = $('token').value; $('token').value = ''; action(refresh);};
$('refresh').onclick = () => action(refresh);
$('send-form').onsubmit = e => {e.preventDefault(); action(async () => {
  const button = e.target.querySelector('button'); button.disabled = true;
  try { await api('/api/requests', {transcription: $('text').value}); $('text').value = ''; await refresh(); }
  finally { button.disabled = false; }
});};
$('disconnect').onclick = () => action(async () => {
  if (confirm('Revoke all Dots connections?')) { await api('/api/dots/disconnect', {}); await refresh(); }
});
setInterval(() => {
  // Do not replace a review control while the user is choosing a destination.
  if (token && !document.hidden && !$('requests').contains(document.activeElement)) {
    refresh().catch(() => { $('last-updated').textContent = 'Status refresh failed. Use Refresh to try again.'; });
  }
}, 15000);
