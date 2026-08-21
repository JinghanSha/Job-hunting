const directions = [
  'Clinical Development', 'Clinical Scientist', 'Clinical Research Physician', 'Medical Affairs', 'MSL',
  'Medical Advisor', 'Translational Medicine', 'Biomarker', 'Clinical Pharmacology', 'Regulatory Affairs',
  'Pharmacovigilance', 'Medical Writing', 'Healthcare Consulting', 'Business Development'
];
const fitLabels = { A: 'A — 高度相关', B: 'B — 相关', C: 'C — 可能适合', D: 'D — 低相关' };

function asText(value) {
  return value === null || value === undefined ? '' : String(value);
}

function escapeHtml(value) {
  return asText(value).replace(/[&<>"']/g, character => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  })[character]);
}

function safeUrl(value) {
  try {
    const parsed = new URL(asText(value));
    return ['http:', 'https:'].includes(parsed.protocol) ? parsed.href : '';
  } catch {
    return '';
  }
}

function getTags(job) {
  return Array.isArray(job?.tags) ? job.tags.map(asText).filter(Boolean) : [];
}

function getJobId(job) {
  return asText(job?.id) || [job?.company, job?.title, job?.city, job?.location].map(asText).join('|');
}

function parseJobDate(value) {
  const text = asText(value).trim();
  if (!text) return null;
  const normalized = /^\d{4}-\d{2}-\d{2}$/.test(text) ? `${text}T00:00:00` : text;
  const parsed = new Date(normalized);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

function isWithinLastDays(value, days) {
  const target = parseJobDate(value);
  if (!target) return false;
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  target.setHours(0, 0, 0, 0);
  const elapsedDays = Math.floor((today - target) / 86_400_000);
  return elapsedDays >= 0 && elapsedDays < days;
}

function jobFirstSeenDate(job) {
  return asText(job?.firstSeen) || asText(job?.date);
}

function formatDate(value) {
  const parsed = parseJobDate(value);
  return parsed ? new Intl.DateTimeFormat('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit' }).format(parsed) : '';
}

function loadFavorites() {
  try {
    const saved = JSON.parse(localStorage.getItem('medicalPhdJobFavorites') || '[]');
    return Array.isArray(saved) ? saved.map(asText) : [];
  } catch {
    return [];
  }
}

const state = { jobs: [], favorites: loadFavorites() };
const elements = {
  stats: document.querySelector('#stats'), search: document.querySelector('#search'), company: document.querySelector('#companyFilter'),
  sort: document.querySelector('#sortOrder'), list: document.querySelector('#jobsList'), empty: document.querySelector('#emptyState'),
  resultCount: document.querySelector('#resultCount'), status: document.querySelector('#dataStatus')
};

function renderDirectionFilters() {
  document.querySelector('#directionFilters').innerHTML = directions.map(direction =>
    `<label class="check-row"><input type="checkbox" name="direction" value="${escapeHtml(direction)}"><span>${escapeHtml(direction)}</span></label>`
  ).join('');
}

function selectedValues(name) {
  return [...document.querySelectorAll(`input[name="${name}"]:checked`)].map(input => input.value);
}

function getFilteredJobs() {
  const search = asText(elements.search.value).trim().toLowerCase();
  const cities = selectedValues('city');
  const selectedDirections = selectedValues('direction');
  const degrees = selectedValues('degree');
  const medicalPhdFits = selectedValues('medicalPhdFit');
  const result = state.jobs.filter(job => {
    const text = [job?.company, job?.title, job?.city, job?.location, job?.direction, job?.major, job?.summary, ...getTags(job)]
      .map(asText).join(' ').toLowerCase();
    const degree = asText(job?.degree);
    return (!search || text.includes(search)) && (!cities.length || cities.includes(asText(job?.city))) &&
      (!selectedDirections.length || selectedDirections.includes(asText(job?.direction))) && (!degrees.length || degrees.some(item => degree.includes(item))) &&
      (!medicalPhdFits.length || medicalPhdFits.includes(asText(job?.medicalPhdFit))) &&
      (!elements.company.value || elements.company.value === asText(job?.company));
  });
  return result.sort((a, b) => {
    const aDate = asText(a?.date) || asText(a?.firstSeen);
    const bDate = asText(b?.date) || asText(b?.firstSeen);
    return elements.sort.value === 'oldest' ? aDate.localeCompare(bDate) : bDate.localeCompare(aDate);
  });
}

function renderStats() {
  const recent = state.jobs.filter(job => isWithinLastDays(jobFirstSeenDate(job), 7)).length;
  const stats = [
    ['当前岗位总数', state.jobs.length],
    ['上海岗位', state.jobs.filter(job => asText(job?.city) === '上海').length],
    ['苏州岗位', state.jobs.filter(job => asText(job?.city) === '苏州').length],
    ['最近新增（7天）', recent]
  ];
  elements.stats.innerHTML = stats.map(([label, value]) =>
    `<div class="stat"><div class="stat-label">${escapeHtml(label)}</div><div class="stat-value">${escapeHtml(value)}</div></div>`
  ).join('');
}

function renderDirectionBoard() {
  const counts = directions.map(direction => [direction, state.jobs.filter(job => asText(job?.direction) === direction).length])
    .filter(([, count]) => count > 0);
  document.querySelector('#directionBoard').innerHTML = counts.map(([direction, count]) =>
    `<div class="direction-item"><span>${escapeHtml(direction)}</span><strong>${escapeHtml(count)}</strong></div>`
  ).join('');
}

function renderJobs() {
  const jobs = getFilteredJobs();
  elements.resultCount.textContent = `${jobs.length} 条`;
  elements.list.innerHTML = jobs.map(job => {
    const id = getJobId(job);
    const favorite = state.favorites.includes(id);
    const fit = asText(job?.medicalPhdFit);
    const fitClass = Object.hasOwn(fitLabels, fit) ? ` fit-${fit}` : '';
    const tags = getTags(job);
    const primaryUrl = safeUrl(job?.url);
    const location = [asText(job?.city), asText(job?.location)].filter(Boolean).join(' · ') || '地点未提供';
    const publishedDate = formatDate(job?.date);
    const lastSeenDate = formatDate(job?.lastSeen);
    const badges = [
      job?.verified === true ? '<span class="sample-note">Official ✓</span>' : '',
      isWithinLastDays(jobFirstSeenDate(job), 3) ? '<span class="sample-note">NEW</span>' : ''
    ].join('');
    const link = primaryUrl
      ? `<a class="link" href="${escapeHtml(primaryUrl)}" target="_blank" rel="noopener noreferrer">查看原始链接 ↗</a>`
      : '<span class="link" aria-disabled="true">原始链接不可用</span>';
    return `<article class="job-card">
      <div class="job-top"><div><p class="company">${escapeHtml(job?.company || '公司未提供')}${badges}</p><h2 class="job-title">${escapeHtml(job?.title || '职位名称未提供')}</h2></div>
      <button class="favorite ${favorite ? 'active' : ''}" type="button" data-favorite="${escapeHtml(id)}" aria-label="${escapeHtml(`${favorite ? '取消收藏' : '收藏'} ${job?.title || '岗位'}`)}" title="${escapeHtml(favorite ? '取消收藏' : '收藏')}">${favorite ? '♥' : '♡'}</button></div>
      <div class="job-meta"><span>${escapeHtml(location)}</span><span>${escapeHtml(job?.direction || '方向未提供')}</span><span>${escapeHtml(job?.degree || '学历未提供')}</span><span>${escapeHtml(job?.experience || '经验未提供')}</span><span>${escapeHtml(job?.salary || '薪资未披露')}</span><span>${publishedDate ? `发布于 ${escapeHtml(publishedDate)}` : '发布日期未提供'}</span>${lastSeenDate ? `<span>最后更新于 ${escapeHtml(lastSeenDate)}</span>` : ''}</div>
      <p class="summary">${escapeHtml(job?.summary || '岗位摘要未提供。')}</p>
      <div class="job-bottom"><div class="tags"><span class="fit${fitClass}">${escapeHtml(fitLabels[fit] || '未评估')}</span>${tags.map(tag => `<span class="tag">${escapeHtml(tag)}</span>`).join('')}</div>${link}</div>
    </article>`;
  }).join('');
  elements.empty.hidden = jobs.length > 0;
}

function populateCompanies() {
  const companies = [...new Set(state.jobs.map(job => asText(job?.company)).filter(Boolean))].sort();
  elements.company.innerHTML = '<option value="">全部公司</option>' + companies.map(company =>
    `<option value="${escapeHtml(company)}">${escapeHtml(company)}</option>`
  ).join('');
}

function saveFavorites() { localStorage.setItem('medicalPhdJobFavorites', JSON.stringify(state.favorites)); }

document.addEventListener('change', renderJobs);
elements.search.addEventListener('input', renderJobs);
document.querySelector('#clearFilters').addEventListener('click', () => {
  document.querySelectorAll('input[type="checkbox"]').forEach(input => { input.checked = false; });
  elements.search.value = ''; elements.company.value = ''; elements.sort.value = 'newest'; renderJobs();
});
elements.list.addEventListener('click', event => {
  const button = event.target.closest('[data-favorite]');
  if (!button) return;
  const id = asText(button.dataset.favorite);
  state.favorites = state.favorites.includes(id) ? state.favorites.filter(item => item !== id) : [...state.favorites, id];
  saveFavorites(); renderJobs();
});

async function init() {
  renderDirectionFilters();
  try {
    const response = await fetch('data/jobs.json');
    if (!response.ok) throw new Error('无法读取岗位数据');
    const data = await response.json();
    state.jobs = Array.isArray(data) ? data.filter(job => job && typeof job === 'object') : [];
    renderStats(); renderDirectionBoard(); populateCompanies(); renderJobs();
    elements.status.textContent = `${state.jobs.length} 条岗位 · 数据源 data/jobs.json`;
  } catch (error) {
    elements.status.textContent = '数据加载失败，请通过本地服务器运行';
    elements.empty.hidden = false;
    elements.empty.querySelector('h2').textContent = '暂时无法加载岗位数据';
  }
}

init();
