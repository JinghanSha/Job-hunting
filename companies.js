const companyState = { companies: [] };
const companyTypes = ['Big Pharma', 'Biotech', 'CRO', 'Medical Device', 'Diagnostics', 'Healthcare Consulting'];
const companyElements = {
  search: document.querySelector('#companySearch'),
  city: document.querySelector('#cityFilter'),
  type: document.querySelector('#typeFilter'),
  list: document.querySelector('#companyList'),
  empty: document.querySelector('#companyEmpty'),
  status: document.querySelector('#companyStatus')
};

function escapeHTML(value) {
  return String(value ?? '').replace(/[&<>'"]/g, character => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[character]));
}

function populateSelect(element, values, label) {
  element.innerHTML = `<option value="">全部${label}</option>` + values.sort().map(value =>
    `<option value="${escapeHTML(value)}">${escapeHTML(value)}</option>`
  ).join('');
}

function getFilteredCompanies() {
  const query = companyElements.search.value.trim().toLowerCase();
  return companyState.companies.filter(company => {
    const searchable = [company.company, company.ChineseName, company.city, company.district, company.companyType, company.therapeuticAreas, company.notes].join(' ').toLowerCase();
    return (!query || searchable.includes(query)) && (!companyElements.city.value || company.city === companyElements.city.value) &&
      (!companyElements.type.value || company.companyType === companyElements.type.value);
  });
}

function renderCompanies() {
  const companies = getFilteredCompanies();
  companyElements.status.textContent = `${companies.length} / ${companyState.companies.length} 家公司`;
  companyElements.list.innerHTML = companies.map(company => `<article class="company-card">
    <div class="company-card-top"><div><p class="company-chinese-name">${escapeHTML(company.ChineseName)}</p><h2 class="company-name">${escapeHTML(company.company)}</h2></div><span class="company-type">${escapeHTML(company.companyType)}</span></div>
    <p class="company-place">${escapeHTML(company.city)} · ${escapeHTML(company.district)}</p>
    <dl><dt>Therapeutic Areas</dt><dd>${escapeHTML(company.therapeuticAreas)}</dd></dl>
    <p class="company-card-note">${escapeHTML(company.notes)}</p>
    <div class="company-links"><a href="${escapeHTML(company.careerWebsite)}" target="_blank" rel="noopener noreferrer">Career Website ↗</a><a href="${escapeHTML(company.LinkedIn)}" target="_blank" rel="noopener noreferrer">LinkedIn ↗</a></div>
  </article>`).join('');
  companyElements.empty.hidden = companies.length > 0;
}

function clearCompanyFilters() {
  companyElements.search.value = '';
  companyElements.city.value = '';
  companyElements.type.value = '';
  renderCompanies();
}

document.addEventListener('DOMContentLoaded', async () => {
  try {
    const response = await fetch('data/companies.json');
    if (!response.ok) throw new Error('无法读取公司数据');
    companyState.companies = await response.json();
    populateSelect(companyElements.city, [...new Set(companyState.companies.map(company => company.city))], '城市');
    populateSelect(companyElements.type, companyTypes, '类型');
    renderCompanies();
  } catch (error) {
    companyElements.status.textContent = '数据加载失败，请通过本地服务器运行';
    companyElements.empty.hidden = false;
  }

  companyElements.search.addEventListener('input', renderCompanies);
  companyElements.city.addEventListener('change', renderCompanies);
  companyElements.type.addEventListener('change', renderCompanies);
  document.querySelector('#clearCompanyFilters').addEventListener('click', clearCompanyFilters);
});
