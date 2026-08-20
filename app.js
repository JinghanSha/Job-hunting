const directions = [
  'Clinical Development', 'Clinical Scientist', 'Clinical Research Physician', 'Medical Affairs', 'MSL',
  'Medical Advisor', 'Translational Medicine', 'Biomarker', 'Clinical Pharmacology', 'Regulatory Affairs',
  'Pharmacovigilance', 'Medical Writing', 'Healthcare Consulting', 'Business Development'
];
const fitLabels = { A: 'A — 高度相关', B: 'B — 相关', C: 'C — 可能适合', D: 'D — 低相关' };

const state = { jobs: [], favorites: JSON.parse(localStorage.getItem('medicalPhdJobFavorites') || '[]') };
const elements = {
  stats: document.querySelector('#stats'), search: document.querySelector('#search'), company: document.querySelector('#companyFilter'),
  sort: document.querySelector('#sortOrder'), list: document.querySelector('#jobsList'), empty: document.querySelector('#emptyState'),
  resultCount: document.querySelector('#resultCount'), status: document.querySelector('#dataStatus')
};

function formatDate(value) {
  const date = new Date(`${value}T00:00:00`);
  return new Intl.DateTimeFormat('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit' }).format(date);
}

function renderDirectionFilters() {
  document.querySelector('#directionFilters').innerHTML = directions.map(direction =>
    `<label class="check-row"><input type="checkbox" name="direction" value="${direction}"><span>${direction}</span></label>`
  ).join('');
}

function selectedValues(name) {
  return [...document.querySelectorAll(`input[name="${name}"]:checked`)].map(input => input.value);
}

function getFilteredJobs() {
  const search = elements.search.value.trim().toLowerCase();
  const cities = selectedValues('city');
  const selectedDirections = selectedValues('direction');
  const degrees = selectedValues('degree');
  const medicalPhdFits = selectedValues('medicalPhdFit');
  const result = state.jobs.filter(job => {
    const text = [job.company, job.title, job.city, job.location, job.direction, job.major, job.summary, ...job.tags].join(' ').toLowerCase();
    return (!search || text.includes(search)) && (!cities.length || cities.includes(job.city)) &&
      (!selectedDirections.length || selectedDirections.includes(job.direction)) && (!degrees.length || degrees.some(degree => job.degree.includes(degree))) &&
      (!medicalPhdFits.length || medicalPhdFits.includes(job.medicalPhdFit)) &&
      (!elements.company.value || elements.company.value === job.company);
  });
  return result.sort((a, b) => elements.sort.value === 'oldest' ? a.date.localeCompare(b.date) : b.date.localeCompare(a.date));
}

function renderStats() {
  const recentDate = new Date('2026-08-13T00:00:00');
  const recent = state.jobs.filter(job => new Date(`${job.date}T00:00:00`) >= recentDate).length;
  const stats = [['当前岗位总数', state.jobs.length], ['上海岗位', state.jobs.filter(job => job.city === '上海').length], ['苏州岗位', state.jobs.filter(job => job.city === '苏州').length], ['最近新增（7天）', recent]];
  elements.stats.innerHTML = stats.map(([label, value]) => `<div class="stat"><div class="stat-label">${label}</div><div class="stat-value">${value}</div></div>`).join('');
}

function renderDirectionBoard() {
  const counts = directions.map(direction => [direction, state.jobs.filter(job => job.direction === direction).length])
    .filter(([, count]) => count > 0);
  document.querySelector('#directionBoard').innerHTML = counts.map(([direction, count]) =>
    `<div class="direction-item"><span>${direction}</span><strong>${count}</strong></div>`
  ).join('');
}

function renderJobs() {
  const jobs = getFilteredJobs();
  elements.resultCount.textContent = `${jobs.length} 条`;
  elements.list.innerHTML = jobs.map(job => {
    const favorite = state.favorites.includes(job.id);
    return `<article class="job-card">
      <div class="job-top"><div><p class="company">${job.company}${job.sample ? '<span class="sample-note">SAMPLE</span>' : ''}</p><h2 class="job-title">${job.title}</h2></div>
      <button class="favorite ${favorite ? 'active' : ''}" type="button" data-favorite="${job.id}" aria-label="${favorite ? '取消收藏' : '收藏'} ${job.title}" title="${favorite ? '取消收藏' : '收藏'}">${favorite ? '♥' : '♡'}</button></div>
      <div class="job-meta"><span>${job.city} · ${job.location}</span><span>${job.direction}</span><span>${job.degree}</span><span>${job.experience}</span><span>${job.salary || '薪资未披露'}</span><span>${job.date ? `发布于 ${formatDate(job.date)}` : '发布日期未提供'}</span></div>
      <p class="summary">${job.summary}</p>
      <div class="job-bottom"><div class="tags"><span class="fit fit-${job.medicalPhdFit}">${fitLabels[job.medicalPhdFit] || '未评估'}</span>${job.tags.map(tag => `<span class="tag">${tag}</span>`).join('')}</div><a class="link" href="${job.url}" target="_blank" rel="noopener noreferrer">查看原始链接 ↗</a></div>
    </article>`;
  }).join('');
  elements.empty.hidden = jobs.length > 0;
}

function populateCompanies() {
  const companies = [...new Set(state.jobs.map(job => job.company))].sort();
  elements.company.innerHTML = '<option value="">全部公司</option>' + companies.map(company => `<option value="${company}">${company}</option>`).join('');
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
  const id = button.dataset.favorite;
  state.favorites = state.favorites.includes(id) ? state.favorites.filter(item => item !== id) : [...state.favorites, id];
  saveFavorites(); renderJobs();
});

async function init() {
  renderDirectionFilters();
  try {
    const response = await fetch('data/jobs.json');
    if (!response.ok) throw new Error('无法读取岗位数据');
    state.jobs = await response.json();
    renderStats(); renderDirectionBoard(); populateCompanies(); renderJobs();
    elements.status.textContent = `${state.jobs.length} 条岗位 · 数据源 data/jobs.json`;
  } catch (error) {
    elements.status.textContent = '数据加载失败，请通过本地服务器运行';
    elements.empty.hidden = false;
    elements.empty.querySelector('h2').textContent = '暂时无法加载岗位数据';
  }
}

init();
