/* UI regression checks. All business APIs are mocked; no live model or database calls.
 * Run with Playwright installed: node scripts/ui-smoke.cjs
 * Optional: CHROME_EXECUTABLE=/path/to/chrome UI_SCREENSHOTS=/path/to/output
 */
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const { chromium } = require('playwright');
const root = path.resolve(__dirname, '../app/static');
const PAGES = {'/':'index.html','/admin':'admin.html','/kb':'kb.html','/review':'review.html','/rag-eval':'rageval.html','/observability':'observability.html','/topics':'topics.html','/topics/questions':'topic-questions.html','/acceptance':'acceptance.html','/acceptance/eval':'acceptance-eval.html','/acceptance/data':'acceptance-data.html','/acceptance/errors':'acceptance-errors.html'};
const out = process.env.UI_SCREENSHOTS;
if (out) fs.mkdirSync(out, {recursive:true});
const modules = [
  {key:'kb',title:'知识库',page:'/kb',status:'ok',lede:'文档切块、知识管理与检索测试',headline:'知识库已就绪，MySQL 与 Milvus 双写一致',metrics:[{label:'知识块',value:128},{label:'待向量化',value:0}],note:'演示接口数据，用于验证页面显示'},
  {key:'rageval',title:'RAG 评估',page:'/rag-eval',status:'ok',lede:'四种检索策略对照，查看召回与回答质量',headline:'最近一次评估已完成',metrics:[{label:'检索策略',value:4}],note:'报表来自最近一次生成结果'},
  {key:'review',title:'飞轮待审',page:'/review',status:'attention',lede:'收集知识缺口，审核答案并写回知识库',headline:'3 条知识缺口等待审核',metrics:[{label:'待审',value:3},{label:'通过',value:12}],note:'人工审核后写回知识库'},
  {key:'observability',title:'观测与成本',page:'/observability',status:'ok',lede:'查看意图成本、质量趋势与置信度校准',headline:'评估趋势与成本报表已生成',metrics:[{label:'评估轮次',value:5}],note:'进入模块查看完整结果'},
  {key:'topics',title:'主题分布',page:'/topics',status:'ok',lede:'按问题主题发现知识缺口与服务需求',headline:'已归类问题覆盖 8 个主题',metrics:[{label:'已归类',value:42},{label:'主题',value:8}],note:'分类器对低置信度问题旁路归类'},
  {key:'classifier',title:'分类器验收',page:'/acceptance',status:'missing',lede:'查看训练数据、模型评测与错例分析',headline:'尚未生成完整验收产物',metrics:[{label:'验收项',value:9}],note:'可进入实验页面生成数据与产物'},
];
const source = {n:1,section_path:'售后政策 / 七天无理由退货',question:'退换货政策是怎样的？',answer:'收到商品后七天内可申请无理由退货，商品须保持完好。具体适用条件以政策为准。'};
const reviewItems = [
  {id:1,normalized_question:'智能饮水机滤芯多久更换一次？',ai_suggested_answer:'建议根据使用频率定期更换滤芯，具体周期以产品说明为准。',occurrence_count:8,review_status:'待审',created_at:'2026-10-08T04:00:00'},
  {id:2,normalized_question:'会员生日优惠可以与满减活动叠加吗？',ai_suggested_answer:'请核对当前会员权益与活动规则后补充准确说明。',occurrence_count:5,review_status:'待审',created_at:'2026-10-08T03:00:00'},
  {id:3,normalized_question:'偏远地区的退货运费如何计算？',ai_suggested_answer:'请按现行运费政策和售后规则核准回答。',occurrence_count:3,review_status:'待审',created_at:'2026-10-08T02:00:00'},
];
const sse = (...items) => items.map(i=>'data: '+(typeof i==='string'?i:JSON.stringify(i))+'\n\n').join('');
const server = http.createServer((req,res)=>{
  const url = new URL(req.url, 'http://localhost');
  const filename = PAGES[url.pathname] || (url.pathname.startsWith('/static/') ? url.pathname.slice(8) : '');
  const file = path.resolve(root, filename);
  if (!filename || !file.startsWith(root+path.sep) || !fs.existsSync(file)) {res.writeHead(404);res.end();return;}
  res.setHeader('Content-Type', {'.html':'text/html; charset=utf-8','.css':'text/css','.js':'text/javascript','.svg':'image/svg+xml'}[path.extname(file)] || 'application/octet-stream');
  res.end(fs.readFileSync(file));
});
(async()=>{
  let browser;
  const checks=[];
  const check = (name,condition) => {assert(condition,name);checks.push(name);};
  try {
    await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
    const base = 'http://127.0.0.1:'+server.address().port;
    browser=await chromium.launch({headless:true,executablePath:process.env.CHROME_EXECUTABLE || undefined,args:['--no-sandbox']});
    const context=await browser.newContext({viewport:{width:1440,height:960}});
    const requests=[];
    await context.route('**/api/**',async route=>{
      const req=route.request(), p=new URL(req.url()).pathname;
      const body=req.method()==='POST'?req.postDataJSON():null;
      requests.push({path:p,body});
      const json=d=>route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(d)});
      if(p==='/api/admin/overview')return json({modules});
      if(p==='/api/kb/overview')return json({chunks:{total:128,pending:0,done:128,key_clause:16,by_content_type:{faq:48,manual:80}},recent:[],staging:{counts:{extracted:3,kept:12,discarded:2},total:17,batches:2,latest_batch:'DEMO-02'},db_error:null,milvus:{online:true,count:128,collection:'knowledge'},consistent:true,sources:[{path:'data/kb/returns-policy.md',file:'returns-policy.md',content_type:'manual',present:true,chars:1400,lines:35,chunks:12,key_clause:4,features:{table_split:false,overlap:true,sections:5}}],content_types:[{key:'faq',desc:'常见问答与服务说明'},{key:'manual',desc:'政策、产品与售后手册'}],jobs:[]});
      if(p==='/api/review/queue'){const status=new URL(req.url()).searchParams.get('status');return json({items:reviewItems.filter(i=>!status||i.review_status===status)});}
      if(p.match(/^\/api\/review\/\d+$/)){const item=reviewItems.find(i=>i.id===Number(p.split('/').pop()));return json({...item,approved_answer:null,raws:[{raw_question:item.normalized_question,source:'retrieval_low_conf',reason:'证据不足',created_at:item.created_at,retrieved_chunks:[{question:source.question,answer:source.answer,section_path:source.section_path,rerank_score:.22}]}]});}
      if(p.match(/^\/api\/review\/\d+\/(approve|reject)$/)){const parts=p.split('/');reviewItems.find(i=>i.id===Number(parts[3])).review_status=parts[4]==='approve'?'通过':'驳回';return json({ok:true,chunk_ids:[129]});}
      if(p==='/api/conversations')return json({items:[{id:42,preview:'订单 1001 的物流查询',has_summary:true},{id:43,preview:'会员权益咨询',has_summary:false}]});
      if(p.match(/^\/api\/conversations\/\d+\/messages$/))return json({items:[{role:'user',content:'订单 1001 到哪了？'},{role:'assistant',content:'该订单的物流信息已经查到。'}]});
      if(p==='/api/chat'){
        if(body.message.includes('错误')) return route.fulfill({contentType:'text/event-stream',body:'event: error\ndata: {"message":"数据库暂时不可用，请稍后重试"}\n\ndata: [DONE]\n\n'});
        if(body.message.includes('退款')) return route.fulfill({contentType:'text/event-stream',body:sse({event:'interrupt',kind:'select_order',conversation_id:42,orders:[{order_id:'1001',product:'自动饮水机',status:'已签收',amount:199}]},'[DONE]')});
        if(body.message.includes('建工单')) return route.fulfill({contentType:'text/event-stream',body:sse({event:'interrupt',kind:'confirm_ticket',conversation_id:42,preview:{ticket_type:'售后',description:'饮水机配件问题需要售后协助'}},'[DONE]')});
        return route.fulfill({contentType:'text/event-stream',body:sse({event:'citations',items:[source]},{event:'tool',name:'query_order'},{delta:'可以为你查询订单，也可以根据政策处理售后。\n\n**退货说明**：收到商品后七天内可申请无理由退货 [1]。'},{event:'done',conversation_id:42},'[DONE]')});
      }
      if(p==='/api/actions/resume'){
        return route.fulfill({contentType:'text/event-stream',body:body.order_id?sse({event:'citations',items:[source]},{delta:'请核对订单信息，确认后提交退款工单 [1]。'},{event:'actions',items:[{type:'refund_form',draft:{order_id:body.order_id}}]},{event:'done',conversation_id:42},'[DONE]'):sse({event:'tool',name:'create_ticket'},{delta:body.confirmed?'工单已创建：TK-DEMO-001':'已取消创建，本次没有提交工单。'},{event:'done',conversation_id:42},'[DONE]')});
      }
      if(p==='/api/actions/create-refund')return json({ticket_no:'RF-DEMO-001'});
      if(p==='/api/actions/create-ticket')return json({ticket_no:'TK-DEMO-002'});
      if(p==='/api/feedback')return json({ok:true});
      return route.fulfill({status:503,contentType:'application/json',body:'{"detail":"测试环境未连接外部服务"}'});
    });
    const page=await context.newPage();
    const errors=[];page.on('pageerror',e=>errors.push(e.message));
    for (const width of [1440,1024,768,390,320]) {
      await page.setViewportSize({width,height:960});
      for(const pathname of Object.keys(PAGES)){
        await page.goto(base+pathname);await page.waitForSelector('.studio-shell');
        const overflow=await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1);
        check('no root overflow '+pathname+' at '+width,!overflow);
        check('active navigation '+pathname+' at '+width,await page.locator('.studio-link.active').count()===1);
        if(out && [1440,390].includes(width) && ['/','/admin','/kb','/review'].includes(pathname)){
          await page.screenshot({path:path.join(out,(pathname==='/'?'chat':pathname.slice(1))+'-'+width+'.png'),fullPage:true});
        }
      }
    }
    check('all pages have no JS errors',errors.length===0);
    await page.setViewportSize({width:1440,height:960});await page.goto(base+'/');
    const send = async text=>{await page.locator('#input').fill(text);await page.locator('#send').click();await page.waitForFunction(()=>!document.getElementById('send').disabled);};
    await send('退换货政策是什么？');
    check('stream answer rendered',await page.locator('.bubble-text').last().textContent().then(t=>t.includes('退货说明')));
    check('source inspector reflects SSE',await page.locator('#source-count').textContent()==='1');
    check('tool inspector reflects SSE',await page.locator('#tool-count').textContent()==='1');
    check('citation decoration retained',await page.locator('.cite').count()===1);
    await page.locator('.cite').click();check('citation popover opens',await page.locator('.cite-pop').isVisible());
    await page.locator('.fb-btn').first().click();check('feedback API called',requests.some(r=>r.path==='/api/feedback'));
    if(out)await page.screenshot({path:path.join(out,'chat-answer.png'),fullPage:true});
    await page.locator('#new-chat').click();check('new chat clears conversation',await page.evaluate(()=>localStorage.getItem('mewhelp_conversation_id'))===null);
    await page.locator('#conversation-search').fill('会员');check('conversation filter',await page.locator('.conv-item').count()===1);
    await page.locator('.conv-item').click();check('history reload',await page.locator('#inspection-state').textContent()==='历史回看');
    await page.locator('#new-chat').click();
    await send('我要退款');check('order selection appears',await page.locator('.order-card').count()===1);
    await page.locator('.order-card').click();await page.waitForFunction(()=>!document.getElementById('send').disabled);
    check('order resume payload',requests.some(r=>r.path==='/api/actions/resume'&&r.body.order_id==='1001'));
    await page.getByRole('button',{name:'提交退款工单',exact:true}).click();
    check('refund dialog opens',await page.locator('#refundMask').isVisible());
    await page.locator('#refundReason').selectOption('质量问题');await page.locator('#refundSubmit').click();
    await page.waitForFunction(()=>!document.getElementById('refundMask').classList.contains('open'));
    check('refund payload',requests.some(r=>r.path==='/api/actions/create-refund'&&r.body.order_id==='1001'&&r.body.reason==='质量问题'));
    await page.locator('#new-chat').click();await send('请帮我建工单');
    check('ticket confirmation appears',await page.locator('.ticket-confirm').count()===1);
    await page.getByRole('button',{name:'确认提交',exact:true}).click();await page.waitForFunction(()=>!document.getElementById('send').disabled);
    check('confirmation resume payload',requests.some(r=>r.path==='/api/actions/resume'&&r.body.confirmed===true));
    await page.locator('#new-chat').click();await send('请帮我建工单');await page.getByRole('button',{name:'取消',exact:true}).click();await page.waitForFunction(()=>!document.getElementById('send').disabled);
    check('cancel resume payload',requests.some(r=>r.path==='/api/actions/resume'&&r.body.confirmed===false));
    await send('错误测试');check('structured stream error shown',(await page.locator('.bubble.error').last().textContent()).includes('数据库暂时不可用'));
    const parser=await page.evaluate(async()=>{
      const frame='data: {"delta":"中文测试"}\r\n\r\ndata: [DONE]\r\n\r\n';
      const bytes=new TextEncoder().encode(frame);let answer='';
      const stream=new ReadableStream({start(c){for(let i=0;i<bytes.length;i+=3)c.enqueue(bytes.slice(i,i+3));c.close();}});
      await readSSEStream(new Response(stream),{delta:s=>answer+=s});
      let truncated=false;try{await readSSEStream(new Response('data: {"delta":"半句"}\n\n'),{delta:()=>{}});}catch(e){truncated=e.message.includes('提前结束');}
      return {answer,truncated};
    });
    check('SSE handles fragmented UTF-8 and CRLF',parser.answer==='中文测试');check('SSE detects premature EOF',parser.truncated);
    await page.goto(base+'/review');await page.waitForSelector('.list .card');
    await page.locator('.row-main').first().click();await page.waitForSelector('.card.open');
    check('review detail expands',await page.locator('.card.open .detail').textContent().then(t=>t.includes('归并原话')));
    await page.locator('.list').getByRole('button',{name:'通过',exact:true}).first().click();check('review dialog opens',await page.locator('#mask').isVisible());
    await page.locator('#modalAnswer').fill('经人工核准的滤芯更换说明');await page.locator('#modalOk').click();await page.waitForFunction(()=>!document.getElementById('mask').classList.contains('open'));
    check('review approval payload',requests.some(r=>r.path==='/api/review/1/approve'&&r.body.approved_answer==='经人工核准的滤芯更换说明'));
    await page.locator('.list').getByRole('button',{name:'驳回',exact:true}).first().click();check('review rejection API',requests.some(r=>r.path==='/api/review/2/reject'));
    await page.setViewportSize({width:390,height:844});await page.goto(base+'/');
    await page.locator('.shell-menu').click();check('mobile navigation opens',await page.locator('.studio-shell').evaluate(e=>e.classList.contains('nav-open')));
    await page.keyboard.press('Escape');check('mobile navigation closes',!await page.locator('.studio-shell').evaluate(e=>e.classList.contains('nav-open')));
    await page.locator('#history-toggle').click();check('mobile history accessible',await page.locator('#sidebar').isVisible());await page.locator('#close-history').click();check('mobile history closes',!await page.locator('#sidebar').isVisible());
    await page.locator('#inspector-toggle').click();check('mobile sources accessible',await page.locator('#inspector').isVisible());await page.locator('#close-inspector').click();
    check('final no JS errors',errors.length===0);
    console.log(JSON.stringify({passed:checks.length,viewports:[1440,1024,768,390,320],pages:Object.keys(PAGES).length,api_mode:'mocked',checks},null,2));
  } catch(e){console.error(e);process.exitCode=1;}
  finally {if(browser)await browser.close();server.close();}
})();
