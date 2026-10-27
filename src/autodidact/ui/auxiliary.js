// 文件职责：六站辅助来源配置与人工采集；自动提问单次确认，所有回答只按0级观察展示。
let auxiliarySnapshot=null, auxiliaryID='aux_deepseek', auxiliaryBusy=false, importAttempt=null;
const auxiliarySelectors=['input_selector','send_selector','assistant_message_selector','new_chat_selector','done_selector'];
// 功能：以纯文本显示结果，外部模型内容从不解释为HTML或控制指令。
function auxiliaryResult(data){el('aux-result').textContent=typeof data==='string'?data:JSON.stringify(data,null,2);}
// 功能：从编辑器读取当前草稿；URL和浏览器资料路径不接受用户任意配置。
function auxiliaryDraft(){
  const draft={id:auxiliaryID,enabled:el('aux-enabled').checked,mode:el('aux-mode').value};
  for(const key of auxiliarySelectors)draft[key]=el('aux-'+key).value.trim();return draft;
}
// 功能：判断草稿是否尚未保存，避免用旧配置偷偷执行网页操作。
function auxiliaryDirty(){
  const saved=auxiliarySnapshot?.sources.find(s=>s.id===auxiliaryID);if(!saved)return false;
  return Object.entries(auxiliaryDraft()).some(([key,value])=>value!==saved[key]);
}
// 功能：保存后的配置与输入草稿分开；切换时丢弃草稿须确认。
async function selectAuxiliary(id){
  if(auxiliaryBusy)return;
  if((auxiliaryDirty()||el('aux-prompt').value||el('aux-response').value||el('aux-ref').value)&&!await askConfirmation('切换来源将清空当前问题、粘贴文本及未保存配置，是否继续？已保存观察保留。'))return;
  auxiliaryID=id;renderAuxiliary(true);
}
// 功能：显示六站选择和模式边界；不把启用、窗口打开或元素检查当成实测成功。
function renderAuxiliary(clearInput=false){
  const selected=auxiliarySnapshot?.sources.find(s=>s.id===auxiliaryID);if(!selected)return;
  el('aux-list').replaceChildren();
  for(const source of auxiliarySnapshot.sources){
    const node=button(source.name+' · '+(source.enabled?'已启用':'未启用'),()=>selectAuxiliary(source.id));
    node.className='source-choice'+(source.id===auxiliaryID?' selected':'');node.setAttribute('aria-pressed',String(source.id===auxiliaryID));el('aux-list').append(node);
  }
  el('aux-name').textContent=selected.name;el('aux-url').textContent=selected.url;el('aux-url').href=selected.url;
  if(window.AUTODIDACT_PREVIEW){el('aux-url').removeAttribute('href');el('aux-url').textContent+='（演示不打开外链）';}
  el('aux-enabled').checked=selected.enabled;el('aux-mode').value=selected.mode;
  el('aux-mode').querySelector('[value="browser"]').disabled=!selected.automatic_supported;
  for(const key of auxiliarySelectors)el('aux-'+key).value=selected[key];
  el('aux-state').textContent=selected.login_open?'人工登录窗口已打开；请完成后关闭。':selected.verification;
  el('aux-boundary').textContent=selected.automatic_supported?
    '默认人工采集可直接留档。网页提问需登录、配置四个必填选择器并核查；已启用自动来源会加入F11可选清单，但不会自行执行。':
    'Claude Code 仅人工采集。请在网站自行控制任务/仓库授权；本项目不创建代码任务、不推送分支、不接受其回答为已验证事实。';
  el('aux-selector-fields').hidden=selected.mode!=='browser';
  el('aux-history').replaceChildren(textNode('p','点击“读取最近观察”查看本来源持久记录；切换来源不自动读库。','muted'));
  if(clearInput){el('aux-prompt').value='';el('aux-response').value='';el('aux-ref').value='';el('aux-consent').checked=false;importAttempt=null;}
  auxiliaryResult('尚未执行操作。配置和模式不代表站点可用；本站不保证免费额度。');
}
// 功能：仅GET读取配置，数据库不可用仍可配置；显式刷新才替换当前草稿。
async function loadAuxiliary(){
  if(auxiliaryBusy)return;
  if(auxiliaryDirty()&&!await askConfirmation('重新加载将丢弃未保存配置，是否继续？'))return;
  try{auxiliarySnapshot=await requestAPI('/api/auxiliary');renderAuxiliary();}
  catch(error){auxiliaryResult(error.message);}
}
// 功能：使用已保存版本执行单个动作；错误不清空问题/回答，不自动重发。
async function auxiliaryAction(action){
  if(auxiliaryBusy)return;
  if(!auxiliarySnapshot){auxiliaryResult('请先加载来源配置。');return;}
  if(action!=='save'&&action!=='close'&&auxiliaryDirty()){auxiliaryResult('配置草稿尚未保存，请先保存或重新加载。');return;}
  let body={id:auxiliaryID,revision:auxiliarySnapshot.revision};
  if(action==='save')body={revision:auxiliarySnapshot.revision,source:auxiliaryDraft()};
  if(action==='close')body={};
  if(['ask','import'].includes(action)){
    if(!el('aux-consent').checked){auxiliaryResult('请先勾选本次操作授权。');return;}
    body={...body,prompt:el('aux-prompt').value.trim(),consent:true};
    if(!body.prompt){auxiliaryResult('请填写研究问题。');return;}
    if(action==='import'){
      body.response=el('aux-response').value.trim();body.conversation_ref=el('aux-ref').value.trim();
      if(!body.response){auxiliaryResult('请粘贴网站回答原文。');return;}
      const signature=JSON.stringify(body);if(importAttempt?.signature!==signature)importAttempt={signature,id:crypto.randomUUID()};
      body.observation_id=importAttempt.id;
    }else if(auxiliarySnapshot.sources.find(s=>s.id===auxiliaryID)?.mode!=='browser'){
      auxiliaryResult('该来源只接受人工采集；如站点支持，可先配置并保存网页提问模式。');return;
    }
  }
  const warnings={login:'在本机打开该站点的独立浏览器窗口？账号和验证由你人工完成，登录资料保存在本机；不会自动发送问题。',
    check:'打开网站检查页面元素？会联网，但不会填写或发送问题。请先关闭登录窗口。',
    ask:'向选定网站发送这一次问题？可能消耗网站额度并产生费用；会建立新会话，回答只按0级观察留档，不晋升信念。超时后请先查记录，不要直接重发。',
    import:'将这段人工粘贴内容保存为0级观察？不会访问网站，也不会核验它确实由所选网站生成。'};
  if(warnings[action]&&!await askConfirmation(warnings[action]))return;
  auxiliaryBusy=true;
  const controls=[...el('view-sources').querySelectorAll('button,input,select,textarea')];
  const disabled=controls.map(n=>n.disabled);controls.forEach(n=>n.disabled=true);
  try{
    const result=await requestAPI('/api/auxiliary/'+action,body);
    if(action==='save'){auxiliarySnapshot=result;renderAuxiliary();}
    if(action==='login')el('aux-state').textContent=window.AUTODIDACT_PREVIEW?'演示：未打开真实登录窗口。':'已打开人工登录窗口（最长15分钟）；是否登录成功需人工确认。';
    if(action==='close')el('aux-state').textContent='本进程登录窗口已关闭；登录资料保留在本机。';
    auxiliaryResult(result);
    if(action==='ask'||action==='import')el('aux-consent').checked=false;
  }catch(error){auxiliaryResult('操作未完成：'+error.message+'\n若已发送问题或保存请求超时，先读取最近观察核对，系统不会自动重试。');}
  finally{auxiliaryBusy=false;controls.forEach((n,i)=>n.disabled=disabled[i]);}
}
// 功能：按当前来源只读加载最近20条观察，恶意回答纯文本展示，记录不是证据等级晋升。
async function loadAuxiliaryHistory(){
  if(auxiliaryBusy)return;const id=auxiliaryID;
  try{
    const data=await requestAPI('/api/auxiliary/history?id='+encodeURIComponent(id));if(id!==auxiliaryID)return;
    el('aux-history').replaceChildren();
    if(!data.items?.length)el('aux-history').append(textNode('p','暂无观察，或尚未发送/导入。','empty'));
    for(const row of data.items||[]){
      const item=textNode('details','','observation-card');item.append(textNode('summary',row.prompt));
      item.append(textNode('p',row.created_at+' · '+row.access_type+' · 0级 · '+row.id,'muted'));
      item.append(textNode('pre',row.response));
      if(row.response_truncated)item.append(textNode('p','回答仅显示前8000字符，完整内容仍保留在数据库。','muted'));
      if(row.conversation_ref)item.append(textNode('p','人工/网页出处声明：'+row.conversation_ref));
      item.append(textNode('pre',JSON.stringify(row.metadata,null,2)));el('aux-history').append(item);
    }
  }catch(error){auxiliaryResult(error.message);}
}
// 功能：初始化入口和表单，不在页面加载时启动浏览器、发送问题或写配置。
function initAuxiliary(){
  el('aux-refresh').onclick=loadAuxiliary;el('aux-history-load').onclick=loadAuxiliaryHistory;
  for(const action of ['save','login','close','check','ask','import'])el('aux-'+action).onclick=()=>auxiliaryAction(action);
  el('aux-mode').onchange=()=>{el('aux-selector-fields').hidden=el('aux-mode').value!=='browser';};
}
