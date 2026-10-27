// 文件职责：仅独立预览页面加载；虚构示例与模拟操作只在内存中存在，绝不转发 API。
window.AUTODIDACT_PREVIEW = true;
(() => {
  const stamp='2026-10-23T08:30:00Z';
  const uid=n=>'00000000-0000-4000-8000-'+String(n).padStart(12,'0');
  const goals=[
    {id:uid(1),title:'低照度下视觉定位的可靠性与失效边界',status:'pending'},
    {id:uid(2),title:'核查惯性导航误差随时间累积的条件',status:'passed'},
    {id:uid(3),title:'调查多传感器融合结论中的来源分歧',status:'blocked'},
  ];
  const beliefs=[
    {id:uid(11),statement:'示例：视觉定位的表现取决于照明、纹理与运动条件。',status:'supported',topic:'视觉定位',confidence:0.72},
    {id:uid(12),statement:'示例：惯性导航误差可能随时间累积，需结合具体条件核查。',status:'provisional',topic:'惯性导航',confidence:0.61},
  ];
  const disputes=[{id:uid(21),status:'open',notes:'示例：不同研究的测试条件是否可直接比较？'}];
  const reports=[
    {id:uid(31),kind:'belief_review',created_at:stamp},
    {id:uid(32),kind:'benchmark_run',created_at:stamp},
    {id:uid(33),kind:'service_run',created_at:stamp},
  ];
  const sources=[{id:uid(41),title:'示例来源 · 传感器条件与局限',url:'https://example.invalid/research',quality_class:'示例，未经核查'}];
  const claims=[{id:uid(51),statement:'示例候选主张：传感器融合可能改善特定测试条件下的定位。',topic:'定位'}];
  const runs=[{id:uid(33),status:'completed',phase:'finished',cycles:1,last_outcome:'passed',updated_at:stamp}];
  let job={status:'idle'}, next=100;
  const auxiliary={revision:'0'.repeat(64),sources:[['deepseek','DeepSeek','https://chat.deepseek.com/'],['zhipu','智谱清言','https://chatglm.cn/'],['kimi','Kimi','https://www.kimi.com/'],['chatgpt','ChatGPT','https://chatgpt.com/'],['claude','Claude 网页聊天','https://claude.ai/new'],['claude_code','Claude Code 网页任务','https://claude.ai/code']].map(([id,name,url])=>({id:'aux_'+id,name,url,enabled:false,mode:'manual',input_selector:'',send_selector:'',assistant_message_selector:'',new_chat_selector:'',done_selector:'',login_open:false,automatic_supported:id!=='claude_code',verification:'演示配置；未访问网站'}))};
  const auxiliaryObservations=[];
  const envelope=data=>({view:{data},note:'虚构演示数据，不代表实际研究或核验结果。',display_truncated:false});
  // 功能：替代所有 fetch，仅返回内存示例；未知接口也不访问真实服务。
  window.fetch=async(path,options={})=>{
    const url=new URL(path,location.origin), method=options.method||'GET';
    let body={};try{body=JSON.parse(options.body||'{}');}catch{return new Response('{"error":"无效输入"}',{status:400});}
    let data, status=200;
    if(url.pathname==='/api/auxiliary')data=auxiliary;
    else if(url.pathname==='/api/auxiliary/history')data={items:auxiliaryObservations.filter(x=>x.provider===url.searchParams.get('id'))};
    else if(url.pathname.startsWith('/api/auxiliary/')){
      const action=url.pathname.split('/').pop();
      if(action==='save'){
        const source=auxiliary.sources.find(s=>s.id===body.source.id);
        if(source?.id==='aux_claude_code'&&body.source.mode==='browser'){status=400;data={error:'Claude Code仅人工采集'};}
        else if(body.source.enabled&&body.source.mode==='browser'&&!['input_selector','send_selector','assistant_message_selector','new_chat_selector'].every(k=>body.source[k])){status=400;data={error:'请配置四个必填选择器'};}
        else{Object.assign(source,body.source);data=auxiliary;}
      }else if(action==='import'){
        if(!auxiliary.sources.find(s=>s.id===body.id)?.enabled){status=400;data={error:'请先启用来源并保存'};}
        else{if(!auxiliaryObservations.some(s=>s.id===body.observation_id))auxiliaryObservations.unshift({id:body.observation_id,provider:body.id,prompt:body.prompt,response:body.response,conversation_ref:body.conversation_ref,access_type:'web_manual',created_at:stamp,metadata:{evidence_level:0,collection_method:'manual_paste',provenance_verified:false,demo:true}});data={observation_id:body.observation_id,note:'仅内存演示留档，刷新即清空；未写数据库。'};}
      }else if(action==='ask')data={evidence_level:0,note:'演示模式未发送问题，未访问真实网站。'};
      else data={note:'演示模式不打开真实浏览器、不联网检查。仅展示操作入口。'};
    }
    else if(url.pathname==='/api/state')data={goals,beliefs,disputes,reports,job,metrics:{total_goals:goals.length,active_beliefs:beliefs.length,open_disputes:disputes.length}};
    else if(url.pathname==='/api/goals'&&method==='POST'){
      const goal={id:uid(next++),title:body.title,status:'pending'};goals.unshift(goal);data={goal_id:goal.id,title:goal.title};
    }else if(url.pathname==='/api/ask'&&method==='POST')data={
      model_explanation_pending_review:'这是界面演示回答，未调用模型。\n\n你可以在回答下方核查引用、进入原文审计，或将知识缺口加入目标池。真实模式会使用已接纳信念解释你的问题，并保留不确定性。',
      saved_beliefs:[{...beliefs[0],sources:[{url:sources[0].url,excerpt:'演示引文：测试结果需要同时考虑照明、纹理与运动条件。本段为虚构示例。'}]}],
      missing_knowledge:['比较低照度与正常照度下的定位误差和适用条件'],open_disputes:1};
    else if(['/api/run','/api/service/start'].includes(url.pathname)&&method==='POST'){
      job={status:'running'};data={status:'queued'};
      setTimeout(()=>{if(job.status==='running'){job={status:'completed',cycles:1};runs.unshift({id:uid(next++),status:'completed',phase:'finished',cycles:1,last_outcome:'passed',updated_at:new Date().toISOString()});}},2500);
    }else if(['/api/stop','/api/service/stop'].includes(url.pathname)&&method==='POST'){job={status:'stopped'};data={status:'stop_after_cycle_requested'};}
    else if(url.pathname==='/api/service/health')data={ready:true,alive:true,note:'仅演示就绪，未检查真实数据库'};
    else if(url.pathname==='/api/service/plan')data={plan:{max_cycles:1,interval_seconds:60,max_runtime_seconds:1800},auto_start:false};
    else if(url.pathname==='/api/service/runs')data={runs,note:'仅本页模拟记录，刷新即重置'};
    else if(url.pathname.startsWith('/api/audit/')){
      const [, , ,kind,id]=url.pathname.split('/');
      const list=({beliefs,sources,claims,disputes,reports,operations:[]})[kind]||[];
      const row=list.find(item=>item.id===id);
      if(id&&!row){status=404;data={error:'演示中没有此详情'};}
      else if(id)data={view:{data:{...row,scope:'示例条件；不构成真实已验证知识',note:'所有内容仅用于界面预览。'},part:'evidence',parts:kind==='sources'?['lineage','text']:['evidence','history'],items:kind==='beliefs'?[{stance:'support',excerpt:'演示原文：表现依赖测试条件。',evidence_level:'示例'}]:[],references:kind==='beliefs'?[{kind:'sources',id:uid(41),label:'查看演示来源'}]:[],pagination:{next_offset:null}},note:'虚构演示记录，未经真实核验。',display_truncated:false};
      else {
        const q=url.searchParams.get('q')||'',state=url.searchParams.get('state')||'';
        const filtered=list.filter(row=>JSON.stringify(row).includes(q)&&(!state||row.status===state));
        const offset=Number(url.searchParams.get('offset')||0),limit=Number(url.searchParams.get('limit')||20);
        data={view:{items:filtered.slice(offset,offset+limit),pagination:{next_offset:offset+limit<filtered.length?offset+limit:null}},note:'虚构演示记录',display_truncated:false};
      }
    }else if(url.pathname==='/api/experiments'){
      const view=url.searchParams.get('view'),windowInfo={window_start_utc:'2026-10-01T00:00:00Z',window_end_utc:stamp,read_at_utc:stamp};
      const result=view==='reports'?{items:reports}:view==='report'?{report_id:url.searchParams.get('report_id'),kind:'演示报告',created_at:stamp,data:{status:'演示',note:'未进行真实实验或评分'}}:view==='benchmark'?{groups:[],items:[],rejected_reports:[]}:{coverage:{sessions:1,recorded_days:1,span_days:0,thirty_day_effect_proven:false},usage:{daily_resources:[]},errors:{operation_errors:[],model_calls:[]},daily_learning:{points:[]},note:'虚构示例；没有真实费用或长期效果数据。'};
      data=envelope({window:windowInfo,data:result});
    }else if(method==='GET')data=envelope({note:'此高级视图在演示模式中没有数据。真实环境将读取对应审计结果。',items:[]});
    else {status=400;data={error:'此高级写入不在演示中模拟，请在配置好的真实工作台使用。'};}
    return new Response(JSON.stringify(data),{status,headers:{'Content-Type':'application/json'}});
  };
})();
