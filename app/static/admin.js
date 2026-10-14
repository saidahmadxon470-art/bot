const $ = (selector) => document.querySelector(selector);
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[char]));
const api = async (url, options = {}) => {
  const isForm = options.body instanceof FormData;
  const headers = isForm ? (options.headers || {}) : {"Content-Type":"application/json",...(options.headers || {})};
  const response = await fetch(url, {...options, headers});
  if (!response.ok) throw new Error((await response.json().catch(()=>({}))).detail || `HTTP ${response.status}`);
  return response.status === 204 ? null : response.json();
};
const status = (value) => {
  const labels = {ACTIVE:"Faol",DRAFT:"Qoralama",PAUSED:"To‘xtatilgan",COMPLETED:"Yakunlangan",EXPIRED:"Muddati tugagan",SUBMITTED:"Ko‘rib chiqilmoqda",PROCESSING:"Ko‘rib chiqilmoqda",MANUAL_REVIEW:"Ko‘rib chiqilmoqda",APPROVED:"Tasdiqlandi",REJECTED:"Rad etildi"};
  return `<span class="status ${esc(value)}">${esc(labels[value] || value)}</span>`;
};
const date = (value) => new Date(value).toLocaleString("uz-UZ", {day:"2-digit",month:"short",hour:"2-digit",minute:"2-digit"});
const localDateTime = (value) => {
  const timestamp = new Date(value);
  return new Date(timestamp.getTime() - timestamp.getTimezoneOffset() * 60000).toISOString().slice(0,16);
};

function showView(name) {
  document.querySelectorAll(".view").forEach((view) => view.classList.toggle("active", view.id === `view-${name}`));
  document.querySelectorAll(".nav-item").forEach((button) => button.classList.toggle("active", button.dataset.view === name));
  const labels = {overview:["OVERVIEW","Dashboard"],tasks:["TASKS","Topshiriqlar"],reviews:["HISOBOTLAR","Hisobotlar"],users:["COMMUNITY","Foydalanuvchilar"],settings:["OPERATIONS","Sozlamalar"],admins:["ACCESS CONTROL","Adminlar"]};
  $("#crumb").textContent = labels[name][0];
  $("#page-title").textContent = labels[name][1];
  if (name === "tasks") loadTasks();
  if (name === "reviews") loadReviews();
  if (name === "users") loadUsers();
  if (name === "settings") loadSettings();
  if (name === "admins") loadAdmins();
}

async function loadOverview() {
  try {
    const [stats, reviews, tasks] = await Promise.all([api("/api/statistics"),api("/api/submissions?review=true"),api("/api/tasks")]);
    const cards = [["FOYDALANUVCHILAR",stats.users,"Barcha akkauntlar",""],["FAOL TOPSHIRIQLAR",stats.active_tasks,"Topshiriq holati","accent"],["HISOBOTLAR",stats.submissions,"Barcha screenshotlar",""],["KO‘RIB CHIQILMOQDA",stats.manual_review,"Admin qarori kutilmoqda","alert"]];
    $("#metrics").innerHTML = cards.map(([label,value,foot,kind]) => `<article class="metric ${kind}"><div class="metric-label">${label}</div><div class="metric-value">${value}</div><div class="metric-foot">${foot}</div></article>`).join("");
    $("#review-count").textContent = reviews.length;
    $("#overview-reviews").innerHTML = reviews.slice(0,5).map((item)=>`<tr><td class="mono">#${item.id}</td><td>User ${item.user_id}</td><td>#${item.task_id}</td><td class="risk-high">Review</td><td>${status(item.status)}</td><td><button class="row-action" data-review="${item.id}">Ochish</button></td></tr>`).join("") || `<tr><td colspan="6" class="empty">Review navbati bo‘sh</td></tr>`;
    $("#overview-tasks").innerHTML = tasks.slice(0,5).map((item)=>`<tr><td>${esc(item.title)}</td><td>${item.is_public ? "Barcha jamoaga" : `Bitta a’zoga · ID ${esc(item.assigned_user_telegram_id || "—")}`}</td><td>${item.reward_points} ⭐</td><td>${date(item.ends_at)}</td><td>${status(item.status)}</td></tr>`).join("") || `<tr><td colspan="5" class="empty">Topshiriqlar hali yaratilmagan</td></tr>`;
  } catch (error) { console.error(error); }
}

async function loadTasks() {
  const tasks = await api("/api/tasks");
  $("#tasks-table").innerHTML = tasks.map((item)=>`<tr><td><b>${esc(item.title)}</b><br><small class="mono">#${item.id}</small></td><td>${item.is_public ? "Barcha jamoaga" : `Bitta a’zoga · ID ${esc(item.assigned_user_telegram_id || "—")}`}</td><td>${item.reward_points} ⭐</td><td>${date(item.ends_at)}</td><td>${item.participants}</td><td>${status(item.status)}</td><td><button class="row-action" data-edit-task="${item.id}">Tahrirlash</button> <button class="row-action" data-task-action="${item.status === "ACTIVE" ? "pause" : "activate"}" data-id="${item.id}">${item.status === "ACTIVE" ? "Pauza" : "Faollashtir"}</button> <button class="row-action" data-delete-task="${item.id}">O‘chirish</button></td></tr>`).join("") || `<tr><td colspan="7" class="empty">Topshiriqlar hali yaratilmagan</td></tr>`;
}

async function loadReviews() {
  const filter = $("#review-filter").value;
  const items = await api(`/api/submissions?${filter === "review" ? "review=true" : filter ? `status_filter=${filter}` : ""}`);
  const details = await Promise.all(items.map((item)=>api(`/api/submissions/${item.id}`)));
  $("#review-list").innerHTML = details.map((item)=>`<article class="review-card"><img class="review-image" src="${esc(item.screenshot_url)}" alt="Submission #${item.id} screenshot"><div class="review-info"><div class="review-title"><span>Submission #${item.id} · Task #${item.task_id}</span>${status(item.status)}</div><div class="review-meta">User ID: ${item.user_id}<br>Similarity: ${item.similarity == null ? "—" : `${Math.round(item.similarity*100)}%`} · Fraud: ${item.fraud_score ?? "—"}<br>OCR: ${esc((item.ocr_text || "Matn aniqlanmadi").slice(0,450))}<br>Sabab: ${esc(item.reason || "—")}</div><div class="review-actions"><button class="approve-button" data-approve="${item.id}">Tasdiqlash</button><button class="reject-button" data-reject="${item.id}">Rad etish</button></div></div></article>`).join("") || `<div class="empty">Mos submission topilmadi</div>`;
}

async function loadUsers() {
  const query = encodeURIComponent($("#user-search").value.trim());
  const users = await api(`/api/users${query ? `?query=${query}` : ""}`);
  $("#users-table").innerHTML = users.map((user)=>`<tr><td><b>${esc(user.username ? `@${user.username}` : user.name || "User")}</b></td><td class="mono">${user.telegram_id}</td><td>${user.balance} ⭐</td><td>${date(user.created_at)}</td><td>${user.blocked ? status("REJECTED") : status("ACTIVE")}</td><td><button class="row-action" data-user-detail="${user.id}">Profil / ball</button> <button class="row-action" data-block="${user.id}" data-blocked="${user.blocked}">${user.blocked ? "Ochish" : "Bloklash"}</button></td></tr>`).join("") || `<tr><td colspan="6" class="empty">Foydalanuvchi topilmadi</td></tr>`;
}

async function loadSettings() {
  const settings = await api("/api/settings");
  Object.entries(settings).forEach(([key,value])=>{const input=$("#settings-form").elements.namedItem(key);if(input)input.value=value});
}

async function loadAdmins() {
  const admins = await api("/api/admins");
  $("#admins-table").innerHTML = admins.map((admin)=>`<tr><td><b>${esc(admin.username)}</b></td><td>${date(admin.created_at)}</td><td>${admin.last_login_at ? date(admin.last_login_at) : "—"}</td><td>${status(admin.is_active ? "ACTIVE" : "PAUSED")}</td><td><button class="row-action" data-admin-toggle="${admin.id}" data-enabled="${admin.is_active}">${admin.is_active ? "O‘chirish" : "Faollashtirish"}</button></td></tr>`).join("") || `<tr><td colspan="5" class="empty">Qo‘shimcha admin hisoblari yo‘q</td></tr>`;
}

document.querySelectorAll("[data-view]").forEach((button)=>button.addEventListener("click",()=>showView(button.dataset.view)));
document.querySelectorAll("[data-go]").forEach((button)=>button.addEventListener("click",()=>showView(button.dataset.go)));
$("#today").textContent = new Intl.DateTimeFormat("uz-UZ",{dateStyle:"medium"}).format(new Date());
const taskForm=$("#task-form");
function setTaskFormMode(editing) {
  taskForm.querySelector(".form-head h2").textContent=editing?"Topshiriqni tahrirlash":"Yangi topshiriq";
  taskForm.querySelector('button[type="submit"]').textContent=editing?"Saqlash":"Topshiriq yaratish";
}
function resetTaskForm() {
  taskForm.reset();
  delete taskForm.dataset.editId;
  setTaskFormMode(false);
  $("#form-message").textContent="";
  syncTaskAudience();
}
$("#new-task-button").addEventListener("click",()=>{resetTaskForm();taskForm.classList.remove("hidden")});
$("#close-task").addEventListener("click",()=>{resetTaskForm();taskForm.classList.add("hidden")});
$("#review-filter").addEventListener("change",loadReviews);
let searchTimer;
$("#user-search").addEventListener("input",()=>{clearTimeout(searchTimer);searchTimer=setTimeout(loadUsers,250)});
function syncTaskAudience() {
  const audience=$("#task-audience").value;
  const field=$("#assigned-user-field");
  field.classList.toggle("hidden",audience!=="false");
  $("#task-user-select").required=audience==="false";
}
$("#task-audience").addEventListener("change",()=>{
  syncTaskAudience();
  if($("#task-audience").value==="false")loadTaskAssignees("");
});
let taskUserSearchTimer;
async function loadTaskAssignees(query, selectedId="") {
  const select=$("#task-user-select");
  const users=await api(`/api/users?blocked=false${query?`&query=${encodeURIComponent(query)}`:""}`);
  select.innerHTML=`<option value="">${users.length?"Foydalanuvchini tanlang":"Foydalanuvchi topilmadi"}</option>`+
    users.map((user)=>`<option value="${user.telegram_id}">${esc(user.name||user.username||"Ismsiz")}${user.username?` (@${esc(user.username)})`:""} · ID ${user.telegram_id}</option>`).join("");
  if(selectedId)select.value=String(selectedId);
}
$("#task-user-search").addEventListener("input",(event)=>{
  clearTimeout(taskUserSearchTimer);
  taskUserSearchTimer=setTimeout(()=>loadTaskAssignees(event.target.value.trim()),250);
});
$("#admin-form").addEventListener("submit",async(event)=>{event.preventDefault();const data=Object.fromEntries(new FormData(event.currentTarget));try{await api("/api/admins",{method:"POST",body:JSON.stringify(data)});event.currentTarget.reset();await loadAdmins()}catch(error){alert(error.message)}});
$("#settings-form").addEventListener("submit",async(event)=>{event.preventDefault();const data=Object.fromEntries(new FormData(event.currentTarget));data.submissions_per_minute=Number(data.submissions_per_minute);data.submissions_per_day=Number(data.submissions_per_day);data.manual_review_confidence=Number(data.manual_review_confidence);data.screenshot_retention_days=Number(data.screenshot_retention_days);try{await api("/api/settings",{method:"PATCH",body:JSON.stringify(data)});$("#settings-message").textContent="Sozlamalar saqlandi"}catch(error){$("#settings-message").textContent=error.message}});
$("#task-form").addEventListener("submit",async(event)=>{
  event.preventDefault();
  const formData=new FormData(event.currentTarget);
  const image=formData.get("advertisement_image");
  const data=Object.fromEntries([...formData.entries()].filter(([key])=>!["advertisement_image","task-user-search"].includes(key)));
  data.reward_points=Number(data.reward_points);data.maximum_participants=data.maximum_participants?Number(data.maximum_participants):null;
  data.is_public=data.is_public==="true";
  data.assigned_user_telegram_id=data.assigned_user_telegram_id?Number(data.assigned_user_telegram_id):null;
  if(data.is_public)data.assigned_user_telegram_id=null;
  data.starts_at=new Date(data.starts_at).toISOString();data.ends_at=new Date(data.ends_at).toISOString();
  const editId=event.currentTarget.dataset.editId;
  try{
    const saved=await api(editId?`/api/tasks/${editId}`:"/api/tasks",{method:editId?"PATCH":"POST",body:JSON.stringify(data)});
    if(image instanceof File&&image.size){const upload=new FormData();upload.append("file",image);await api(`/api/tasks/${saved.id||editId}/image`,{method:"POST",body:upload})}
    const successMessage=editId?"Topshiriq yangilandi":data.is_public?`Topshiriq yaratildi va ${saved.notified_users} foydalanuvchiga yuborildi`:saved.notified_users?"Shaxsiy topshiriq foydalanuvchiga yuborildi va Topshiriqlar bo‘limida ham ko‘rinadi":"Topshiriq yaratildi, lekin xabar yetkazilmadi; foydalanuvchi uni Topshiriqlar bo‘limida ko‘radi";
    if(!editId)resetTaskForm();
    $("#form-message").textContent=successMessage;await loadTasks();await loadOverview()
  }
  catch(error){$("#form-message").textContent=error.message}
});
document.addEventListener("click",async(event)=>{
  const button=event.target.closest("button");if(!button)return;
  try{
    if(button.dataset.taskAction){await api(`/api/tasks/${button.dataset.id}/${button.dataset.taskAction}`,{method:"POST"});await loadTasks();await loadOverview()}
    if(button.dataset.editTask){const task=await api(`/api/tasks/${button.dataset.editTask}`);resetTaskForm();showView("tasks");taskForm.classList.remove("hidden");taskForm.dataset.editId=task.id;setTaskFormMode(true);for(const [key,value] of Object.entries(task)){const input=taskForm.elements.namedItem(key);if(input&&value!=null&&key!=="assigned_user_telegram_id"){input.value=input.type==="datetime-local"?localDateTime(value):value}}syncTaskAudience();if(task.assigned_user_telegram_id)await loadTaskAssignees(String(task.assigned_user_telegram_id),task.assigned_user_telegram_id);taskForm.scrollIntoView({behavior:"smooth",block:"start"})}
    if(button.dataset.deleteTask&&confirm("Topshiriq foydalanuvchilar ro‘yxatidan o‘chiriladi. Hisobotlar tarixi saqlanadi. Davom etasizmi?")){await api(`/api/tasks/${button.dataset.deleteTask}`,{method:"DELETE"});await loadTasks();await loadOverview()}
    if(button.dataset.approve){if(confirm("Submissionni tasdiqlab, task mukofotini berasizmi?")){await api(`/api/submissions/${button.dataset.approve}/approve`,{method:"POST"});await loadReviews();await loadOverview()}}
    if(button.dataset.reject){const reason=prompt("Rad etish sababi:");if(reason){await api(`/api/submissions/${button.dataset.reject}/reject`,{method:"POST",body:JSON.stringify({reason})});await loadReviews();await loadOverview()}}
    if(button.dataset.block){const action=button.dataset.blocked==="true"?"unblock":"block";await api(`/api/users/${button.dataset.block}/${action}`,{method:"POST"});await loadUsers()}
    if(button.dataset.userDetail){const user=await api(`/api/users/${button.dataset.userDetail}`);const amount=prompt(`Balans: ${user.balance} ⭐\nApproval: ${Math.round(user.approval_rate*100)}%\nJami task: ${user.total_submissions}\nBall qo‘shish/ayirish (manfiy ham mumkin):`);if(amount!==null&&Number(amount)!==0){await api(`/api/users/${user.id}/points`,{method:"POST",body:JSON.stringify({amount:Number(amount),description:"Admin adjustment"})});await loadUsers()}}
    if(button.dataset.adminToggle){const enabled=button.dataset.enabled!=="true";await api(`/api/admins/${button.dataset.adminToggle}?enabled=${enabled}`,{method:"PATCH"});await loadAdmins()}
    if(button.dataset.review){showView("reviews")}
  }catch(error){alert(error.message)}
});
loadOverview();