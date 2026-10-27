// 文件职责：工作台导航、结构化展示、对话与目标操作；沿用既有 API，不生成认知事实。
const views = {
  sources: ['辅助认知来源', '连接外部观点，保留证据边界', '独立配置六个网站。回答是待核验线索，不替换主模型或直接改写信念。', 'AUXILIARY SOURCES'],
  overview: ['工作总览', '研究工作总览', '查看研究近况，开始下一次探索。', 'OVERVIEW'],
  research: ['研究对话', '与知识展开对话', '把回答、支持依据与仍然未知的部分放在一起核查。', 'CONVERSATION'],
  run: ['目标与运行', '让研究过程可见', '管理目标，查看实际运行状态和最近持久记录。', 'RESEARCH RUNS'],
  knowledge: ['知识与证据', '沿着证据追溯结论', '浏览主张、信念、来源与争议，保留每一次认识变化。', 'KNOWLEDGE & EVIDENCE'],
  review: ['人工复核', '把判断交回给证据', '冻结依据、核对提议，再逐项确认操作。', 'HUMAN REVIEW'],
  experiments: ['实验与报告', '用可复核的记录观察进展', '按真实时间、模型和条件查看报告；未知不是零。', 'EXPERIMENTS'],
  tools: ['高级工具', '研究工具与维护', '概念关系、多模型审查、研究方法和索引运营。', 'RESEARCH TOOLS'],
};
const stateNames = {idle:'等待研究',queued:'已排队',running:'运行中',completed:'已完成',passed:'已通过',
  failed:'失败',stopped:'已停止',interrupted:'已中断',pending:'待处理',blocked:'已阻塞',active:'活跃',
  archived:'已归档',verified:'已验证',supported:'有支持',provisional:'暂定',disputed:'有争议',
  retracted:'已撤回',open:'开放',investigating:'调查中',unresolved:'未解决',waiting:'轮间等待',
  finished:'已结束',approved:'已批准',rejected:'已拒绝',unknown:'未知',stop_requested:'已请求中断',
  stop_after_cycle_requested:'已请求轮后停止'};
const fieldNames = {...labels, id:'记录 ID', goal_id:'目标 ID', job_id:'索引任务 ID', run_id:'运行 ID',
  ready:'就绪', alive:'服务存活', database:'数据库', plan:'运行计划', runs:'运行记录', phase:'阶段',
  cycles:'完成轮数', max_cycles:'轮数上限', interval_seconds:'轮间等待（秒）',
  max_runtime_seconds:'最长运行（秒）', last_outcome:'最近结果', updated_at:'更新时间',
  data:'数据', items:'记录', pagination:'分页', total:'合计', protocol:'协议', model:'模型',
  entity_id:'实体 ID', proposal_id:'提议 ID', state:'状态', basis:'冻结依据',
  basis_validity:'依据有效性', proposal_sha256:'提议摘要', decision:'决定', actor:'操作人声明',
  title:'标题', sources:'来源', evidence_level:'证据等级', claim_id:'主张 ID', belief_id:'信念 ID',
  error:'错误', reason:'原因', health:'环境健康', job:'当前任务', worker:'后台工作者'};
const reportNames={belief_review:'信念再核查',benchmark_run:'可靠基准答卷',service_run:'学习运行记录',
  progress:'长期进展报告',mastery_profile:'主题掌握画像',cognitive_review:'多模型认知审查',
  skill_method_experiment:'研究方法对照实验',human_review_proposal:'人工复核提议',
  human_review_decision:'人工复核决定',memory_change_proposal:'记忆维护提议'};
let currentView = 'overview', snapshot = null, refreshPromise = null, servicePromise = null;
let toastTimer, pollTimer, auditLoaded = false, chatBusy = false, goalBusy = false;
let confirmResolve = null, activity = [], pendingWrites = new Set();

// 功能：纯文本显示状态，未知后端枚举原样保留，避免错误归为成功。
function stateText(value){return stateNames[value] || String(value ?? '未知');}
// 功能：仅已知状态使用对应颜色，未知状态仍显示原值。
function badge(value){
  const good=['passed','completed','verified','supported','approved'];
  const bad=['failed','retracted','interrupted','blocked'];
  const live=['queued','running','waiting'];
  return textNode('span',stateText(value),'badge '+(good.includes(value)?'good':bad.includes(value)?'bad':live.includes(value)?'live':'caution'));
}
// 功能：时间按本机显示并标注；无有效时间时不生成虚假日期。
function dateText(value){if(!value)return '时间未知';const d=new Date(value);return Number.isNaN(d.getTime())?String(value):d.toLocaleString('zh-CN',{hour12:false})+'（本机时间）';}
// 功能：短暂显示本页操作提示，不改变后台状态。
function notifyUser(message){el('toast').textContent=message;el('toast').hidden=false;clearTimeout(toastTimer);toastTimer=setTimeout(()=>el('toast').hidden=true,5500);}
// 功能：只保存本页最近50次操作结果，不替代数据库历史，不存储 Token 或浏览器持久会话。
function recordActivity(message,failed=false){
  activity.unshift({at:new Date().toLocaleTimeString('zh-CN',{hour12:false}),message,failed});
  activity=activity.slice(0,50);el('activity-list').replaceChildren(...activity.map(item=>{
    const row=textNode('li','',item.failed?'warning':'');row.append(textNode('time',item.at),textNode('span',item.message));return row;
  }));
}
// 功能：统一错误、超时和重复写入反馈；超时不自动重发，避免重复计费或重复目标。
async function requestAPI(path,body){
  const write=body!==undefined;
  if(write&&pendingWrites.has(path))throw Error('同一操作仍在处理中，请等待结果。');
  if(write)pendingWrites.add(path);
  const trigger=document.activeElement?.tagName==='BUTTON'?document.activeElement:null;
  const pending=Number(trigger?.dataset.pending||0);
  if(trigger){trigger.dataset.pending=String(pending+1);trigger.setAttribute('aria-busy','true');}
  const controller=new AbortController(), timeout=setTimeout(()=>controller.abort(),write?180000:35000);
  try{
    const response=await fetch(path,{method:write?'POST':'GET',headers:{'X-Autodidact-Token':token,'Content-Type':'application/json'},body:write?JSON.stringify(body):undefined,signal:controller.signal});
    const result=await response.json();
    if(!response.ok)throw Error(result.error||('请求失败（'+response.status+'）'));
    if(write){recordActivity(path+' · '+stateText(result.status||'请求已返回'));notifyUser('操作已返回，请查看本区结果。');}
    return result;
  }catch(error){
    const message=error.name==='AbortError'?'请求超时；后台可能仍在执行，请先查看状态，勿直接重试。':error.message;
    if(write){recordActivity(path+' · '+message,true);notifyUser(message);}
    throw Error(message);
  }finally{
    clearTimeout(timeout);if(write)pendingWrites.delete(path);
    if(trigger){trigger.dataset.pending=String(Math.max(0,Number(trigger.dataset.pending)-1));if(trigger.dataset.pending==='0')trigger.removeAttribute('aria-busy');}
  }
}
// 功能：导航采用固定页面白名单，支持前进/后退；查看页面不启动学习。
function navigate(id,updateHash=true){
  if(!Object.hasOwn(views,id))id='overview';const changed=currentView!==id;currentView=id;
  for(const key of Object.keys(views))el('view-'+key).hidden=key!==id;
  for(const nav of document.querySelectorAll('.nav-item')){
    nav.classList.toggle('active',nav.dataset.go===id);
    if(nav.dataset.go===id)nav.setAttribute('aria-current','page');else nav.removeAttribute('aria-current');
  }
  const [title,heading,description,kicker]=views[id];
  el('view-title').textContent=title;el('page-title').textContent=heading;
  el('page-description').textContent=description;el('view-kicker').textContent=kicker;
  document.title=title+' · Autodidact';
  if(updateHash&&location.hash!=='#'+id)history.pushState(null,'','#'+id);
  if(changed)window.scrollTo({top:0,behavior:'instant'});
  if(id==='knowledge'&&!auditLoaded){auditLoaded=true;loadAuditList();}
  if(id==='sources'&&!auxiliarySnapshot)loadAuxiliary();
  if(id==='run'){refreshService();refresh();}
}
// 功能：嵌套结果分字段阅读，层级与记录数有界，原始 JSON 仍可展开核对。
function renderData(host,value,depth=0){
  if(value===null||value===undefined||typeof value!=='object'){host.append(textNode('p',pretty(value)));return;}
  const entries=Array.isArray(value)?value.map((v,i)=>['记录 '+(i+1),v]):Object.entries(value);
  if(!entries.length){host.append(textNode('p','暂无记录 / 未知，不代表已核验。','muted'));return;}
  const grid=textNode('div','','data-grid');host.append(grid);
  for(const [key,item] of entries.slice(0,30)){
    if(item&&typeof item==='object'){
      const group=textNode('details','','data-group');group.append(textNode('summary',fieldNames[key]||key));
      const content=textNode('div','');group.append(content);
      if(depth<3)renderData(content,item,depth+1);else content.append(textNode('pre',pretty(item)));
      host.append(group);
    }else{
      const cell=textNode('div','','data-cell');cell.append(textNode('small',fieldNames[key]||key),textNode('p',(key==='status'||key==='state'||key==='phase')?stateText(item):pretty(item)));grid.append(cell);
    }
  }
  if(entries.length>30)host.append(textNode('p','结构化视图仅显示前30项，请展开原始数据或使用分页。','warning'));
}
// 功能：兼容旧功能反馈入口，高级结果提供结构化视图和原始数据双层展示。
function showResult(id,value){
  const target=el(id);target.textContent=typeof value==='string'?value:JSON.stringify(value,null,2);
  if(['status','answer','added','service-status','experiment-json','audit-budget'].includes(id))return;
  let result=el(id+'-view');
  if(!result){
    const details=textNode('details','');details.append(textNode('summary','查看原始数据'));
    target.before(details);details.append(target);result=textNode('div','','result-view');result.id=id+'-view';details.before(result);
  }
  result.replaceChildren();renderData(result,value);
}
// 功能：卡片仅取接口返回的记录，不把全部信念数量当接纳知识量或准确率。
function renderRecords(id,rows,kind){
  const host=el(id);host.replaceChildren();
  if(!Array.isArray(rows)||!rows.length){host.append(textNode('p','暂无记录。创建目标并完成研究后，相关结果会出现在这里。','empty'));return;}
  for(const row of rows){
    const card=textNode('article','','record'),content=textNode('div','','record-content');
    card.append(textNode('span',kind==='goals'?'↗':kind==='reports'?'▤':'◇','record-icon'),content);
    content.append(textNode('h3',row.title||row.statement||row.notes||reportNames[row.kind]||row.kind||'记录'));
    const meta=[row.created_at?dateText(row.created_at):'',row.id?'ID '+row.id:''].filter(Boolean).join(' · ');
    content.append(textNode('p',meta));
    if(row.id&&kind!=='goals')content.append(button('查看'+(kinds[kind]||'记录')+' →',()=>openDetail(kind,row.id)));
    if(kind==='goals')content.append(button('以此目标提问',()=>{el('question').value=row.title||'';navigate('research');el('question').focus();}));
    if(row.status)card.append(badge(row.status));host.append(card);
  }
}
// 功能：状态刷新去重并保留最后快照；失败显式标记过期而不归零。
async function refreshWorkspace(){
  if(refreshPromise)return refreshPromise;
  refreshPromise=(async()=>{
    try{
      const s=await api('/api/state');snapshot=s;
      for(const kind of ['goals','beliefs','disputes','reports'])renderRecords(kind,s[kind],kind);
      renderRecords('home-goals',(s.goals||[]).slice(0,4),'goals');
      renderRecords('home-reports',(s.reports||[]).slice(0,3),'reports');
      const metrics=s.metrics||{};
      el('metric-goals').textContent=metrics.total_goals??'—';
      el('metric-beliefs').textContent=metrics.active_beliefs??'—';
      el('metric-disputes').textContent=metrics.open_disputes??'—';
      const status=s.job?.status||'unknown';el('metric-job').textContent=stateText(status);
      el('last-update').textContent='更新于 '+new Date().toLocaleTimeString('zh-CN',{hour12:false});
      el('run-summary').replaceChildren(badge(status),textNode('span',s.job?.reason?'原因：'+s.job.reason:'状态来自当前服务进程，不推断具体研究阶段。'));
      const busy=['queued','running'].includes(status);el('run').disabled=busy;el('service-start').disabled=busy;
      el('stop').disabled=!busy;el('service-stop').disabled=!busy;
      show('status',{metrics:s.metrics,job:s.job});
      el('connection-state').textContent=window.AUTODIDACT_PREVIEW?'演示数据 · 已就绪':'本机服务 · 已连接';
      el('connection-state').className='connection ok';el('connection-error').hidden=true;
    }catch(error){
      el('connection-state').textContent='连接异常 · 数据可能过期';el('connection-state').className='connection error';
      el('connection-error').textContent='无法读取工作台：'+error.message+' 请检查服务、数据库配置与显式初始化步骤；右上角可重新刷新。';
      el('connection-error').hidden=false;
      el('run').disabled=true;el('service-start').disabled=true;
    }
  })().finally(()=>{refreshPromise=null;});
  return refreshPromise;
}
// 功能：独立读取健康、计划和持久运行记录，部分失败不隐去其他成功结果。
async function refreshService(){
  if(servicePromise)return servicePromise;
  servicePromise=(async()=>{
    const results=await Promise.allSettled(['/api/service/health','/api/service/plan','/api/service/runs'].map(path=>api(path)));
    const data={};['health','plan','runs'].forEach((key,i)=>data[key]=results[i].status==='fulfilled'?results[i].value:{error:results[i].reason.message});
    show('service-status',data);el('service-summary').replaceChildren();
    renderData(el('service-summary'),{就绪:data.health.ready??'未知',环境检查:data.health.error||data.health.note||'健康请求已返回，请结合就绪状态核对。',运行计划:data.plan.plan||data.plan,说明:'持久记录是最后快照；其中 running 不证明任务此刻仍在运行。'});
    const host=el('run-history');host.replaceChildren();
    for(const row of data.runs.runs||[]){
      const card=textNode('article','','record'),content=textNode('div','','record-content');
      content.append(textNode('h3','运行 '+row.id),textNode('p',dateText(row.updated_at||row.started_at)+' · 已完成 '+(row.cycles??'未知')+' 轮'),
        textNode('p','阶段：'+stateText(row.phase)+' · 最近结果：'+stateText(row.last_outcome)));
      if(row.id)content.append(button('查看运行报告',()=>openDetail('reports',row.id)));card.append(content,badge(row.status));host.append(card);
    }
    if(!host.children.length)host.append(textNode('p',data.runs.error?'运行记录读取失败：'+data.runs.error:'暂无持久运行记录。','empty'));
  })().finally(()=>{servicePromise=null;});return servicePromise;
}
// 功能：确认弹窗支持键盘/Escape，承诺只在用户确认后发送操作。
function askConfirmation(message){
  if(confirmResolve)return Promise.resolve(false);
  el('confirm-message').textContent=message;el('confirm-dialog').returnValue='';
  el('confirm-dialog').showModal();return new Promise(resolve=>{confirmResolve=resolve;});
}
// 功能：新目标只预填，提交成功后清空；写入失败保留输入供用户核对。
function openGoal(title=''){if(!goalBusy){el('goal').value=title;el('added').textContent='';}el('goal-dialog').showModal();el('goal').focus();}
async function submitGoal(event){
  event.preventDefault();if(goalBusy)return;const title=el('goal').value.trim();
  if(!title){el('added').textContent='请先填写研究目标。';return;}
  goalBusy=true;el('add').disabled=true;el('add').textContent='正在保存…';
  try{await api('/api/goals',{title});el('goal-dialog').close();el('goal').value='';navigate('run');await refresh();notifyUser('目标已加入目标池。确认环境后，可开始一轮学习。');}
  catch(error){el('added').textContent=error.message;}
  finally{goalBusy=false;el('add').disabled=false;el('add').textContent='加入目标池';}
}
// 功能：对话内容只以纯文本显示；不把历史对话自动加入模型上下文。
function addMessage(role,text){
  el('chat-empty')?.remove();const row=textNode('article','','message '+role);
  row.append(textNode('div',(role==='user'?'你':'Autodidact · 待核验解释')+' · '+new Date().toLocaleTimeString('zh-CN',{hour12:false}),'message-meta'),textNode('div',text,'message-body'));
  el('chat-messages').append(row);scrollChat();return row;
}
// 功能：将消息容器滚到最新答复，不改变整页滚动位置。
function scrollChat(){const box=el('chat-messages');box.scrollTop=box.scrollHeight;}
// 功能：明确许可后发送单问题，分开展示解释/引用/缺口；失败不丢失原问题。
async function submitQuestion(event){
  event.preventDefault();if(chatBusy)return;
  const question=el('question').value.trim();if(!question){el('question').focus();return;}
  if(!el('ask-consent').checked&&!window.AUTODIDACT_PREVIEW){notifyUser('请先勾选模型调用许可，再发送问题。');el('ask-consent').focus();return;}
  chatBusy=true;el('ask').disabled=true;el('chat-clear').disabled=true;el('ask').textContent='正在回答…';
  addMessage('user',question);el('question').value='';const answer=addMessage('assistant','正在检索已接纳信念并生成解释，请稍候…');
  try{
    const result=await api('/api/ask',{question});show('answer',result);
    answer.querySelector('.message-body').textContent=result.model_explanation_pending_review||'未返回解释；请检查原始响应。';
    const beliefs=Array.isArray(result.saved_beliefs)?result.saved_beliefs:[];
    answer.append(textNode('h3','引用信念 · '+beliefs.length+' 条'));
    if(!beliefs.length)answer.append(textNode('p','本次没有引用已保存信念；不能据此确认解释正确。','warning'));
    for(const belief of beliefs){
      const source=textNode('div','','source-card');source.append(textNode('p',belief.statement||'原结论未提供'),badge(belief.status));
      for(const citation of belief.sources||[]){source.append(textNode('p',citation.excerpt||'未返回引文'),textNode('small',citation.url||'来源地址未知'));}
      if(belief.id)source.append(button('核对信念与原文 →',()=>openDetail('beliefs',belief.id)));answer.append(source);
    }
    const missing=Array.isArray(result.missing_knowledge)?result.missing_knowledge:[];
    if(missing.length){answer.append(textNode('h3','仍待研究'));for(const item of missing)answer.append(button('＋ '+String(item),()=>openGoal(String(item).slice(0,2000))));}
    answer.append(textNode('p','当前开放争议：'+(result.open_disputes??'未知')+'。模型解释不会直接写入信念。','muted'));
  }catch(error){
    answer.classList.add('failed');answer.querySelector('.message-body').textContent='回答未完成：'+error.message;
    answer.append(button('将问题放回输入框',()=>{el('question').value=question;el('question').focus();}));
  }finally{chatBusy=false;el('ask').disabled=false;el('chat-clear').disabled=false;el('ask').textContent='发送问题 ↑';scrollChat();}
}
// 功能：收费或中断动作先展示影响，发出后刷新真实状态；不会自动再次执行。
async function runAction(path,message){
  if(!await askConfirmation(message))return;
  try{const result=await api(path,{});show('status',result);notifyUser(stateText(result.status)+'；已请求刷新实际状态。');await refresh();await refreshService();}
  catch(error){show('status',error.message);}
}
// 功能：仅当前可见总览/运行页轮询；请求不重叠，后台标签页不轮询。
function scheduleRefresh(){
  clearTimeout(pollTimer);pollTimer=setTimeout(async()=>{
    if(!document.hidden&&['overview','run'].includes(currentView)){await refresh();if(currentView==='run')await refreshService();}
    scheduleRefresh();
  },['queued','running'].includes(snapshot?.job?.status)?10000:30000);
}
// 功能：为高级表单保留可见标签及输入提示，不依赖会消失的 placeholder。
function labelAdvancedInputs(){
  for(const input of document.querySelectorAll('.workspace-panel input,.workspace-panel select,.workspace-panel textarea')){
    if(!input.id||input.closest('label')||document.querySelector('label[for="'+input.id+'"]'))continue;
    const label=document.createElement('label');label.htmlFor=input.id;
    label.textContent=input.getAttribute('aria-label')||input.placeholder||(input.id.endsWith('offset')?'分页偏移':input.id.endsWith('days')?'观察天数':input.id.endsWith('state')?'状态筛选':input.id.endsWith('actor')?'操作人声明':input.id.endsWith('kind')?'类型':'选项');
    const wrapper=textNode('div','','field');input.before(wrapper);wrapper.append(label,input);
  }
}
// 功能：装配导航与原有模块，初始化只有只读状态请求；保留全部高级功能入口。
function initWorkspace(){
  initAuxiliary();
  document.querySelector('.skip-link').onclick=event=>{event.preventDefault();el('main-content').focus();};
  document.querySelectorAll('[data-go]').forEach(node=>node.onclick=()=>navigate(node.dataset.go));
  document.querySelectorAll('[data-action="new-goal"]').forEach(node=>node.onclick=()=>openGoal());
  document.querySelectorAll('[data-action="help"]').forEach(node=>node.onclick=()=>el('help-dialog').showModal());
  document.querySelectorAll('[data-close]').forEach(node=>node.onclick=()=>el(node.dataset.close).close());
  document.querySelectorAll('[data-question]').forEach(node=>node.onclick=()=>{el('question').value=node.dataset.question;el('question').focus();});
  window.addEventListener('popstate',()=>navigate(location.hash.slice(1),false));
  window.addEventListener('hashchange',()=>navigate(location.hash.slice(1),false));
  el('confirm-dialog').addEventListener('close',()=>{const resolve=confirmResolve;confirmResolve=null;resolve?.(el('confirm-dialog').returnValue==='confirm');});
  el('goal-form').onsubmit=submitGoal;el('add').onclick=null;el('chat-form').onsubmit=submitQuestion;el('ask').onclick=null;
  el('question').addEventListener('keydown',event=>{if(event.key==='Enter'&&!event.shiftKey&&!event.isComposing){event.preventDefault();el('chat-form').requestSubmit();}});
  el('chat-clear').onclick=async()=>{if(!chatBusy&&await askConfirmation('清空本页对话显示？数据库中的认知记录和模型审计不受影响。')){el('chat-messages').replaceChildren(textNode('p','本页对话已清空，可以开始新的独立提问。','empty'));show('answer','');}};
  el('run').onclick=()=>runAction('/api/run','开始一轮学习？将使用配置的搜索、模型与嵌入服务，可能产生费用；目标由现有控制器选择。');
  el('service-start').onclick=()=>runAction('/api/service/start','执行已加载的有限运行计划？请先点击健康/计划核对轮数和预算，执行可能计费。');
  el('stop').classList.add('danger');el('stop').onclick=()=>runAction('/api/stop','中断本机当前学习？已发送请求可能仍产生费用，已保存证据保留。');
  el('service-stop').onclick=()=>runAction('/api/service/stop','请求本轮结束后停止？当前轮会继续执行至收尾。');
  el('service-refresh').onclick=refreshService;
  el('refresh').onclick=refresh;el('global-refresh').onclick=async()=>{await refresh();if(currentView==='run')await refreshService();if(currentView==='knowledge')await loadAuditList();};
  const originalIndexRun=el('index-run').onclick;el('index-run').onclick=async()=>{if(await askConfirmation('运行索引批次？最多200项，将调用配置的嵌入服务并可能计费。'))await originalIndexRun();};
  labelAdvancedInputs();
  if(window.AUTODIDACT_PREVIEW){el('preview-banner').hidden=false;el('mode-label').textContent='隔离演示工作空间';}
  navigate(location.hash.slice(1)||'overview',false);refresh();scheduleRefresh();
}
initWorkspace();
