"""One synchronous, bounded DOM observation; no actions, requests or hidden state.

Only the card/time/filter classes have been observed on the real platform.
All other selectors must be supplied with an explicitly labelled layout profile.
"""

SNAPSHOT_SCRIPT = r"""p => {
 const fail = code => ({error:code});
 const rendered = e => {
   for(let n=e;n;n=n.parentElement) {
     const s=getComputedStyle(n);
     if(s.display==='none'||s.visibility==='hidden'||s.visibility==='collapse'||
        n.getAttribute('aria-hidden')==='true'||Number(s.opacity)===0) return false;
   }
   return true;
 };
 const visible = e => {
   if (!e || !e.isConnected) return false;
   let r=e.getBoundingClientRect();
   let left=Math.max(0,r.left), right=Math.min(innerWidth,r.right);
   let top=Math.max(0,r.top), bottom=Math.min(innerHeight,r.bottom);
   for(let n=e;n;n=n.parentElement) {
     const s=getComputedStyle(n);
     if(s.display==='none'||s.visibility==='hidden'||s.visibility==='collapse'||
        n.getAttribute('aria-hidden')==='true'||Number(s.opacity)===0) return false;
     if(n!==e && /(auto|scroll|hidden|clip)/.test(s.overflow+s.overflowX+s.overflowY)) {
       const b=n.getBoundingClientRect();
       left=Math.max(left,b.left);right=Math.min(right,b.right);
       top=Math.max(top,b.top);bottom=Math.min(bottom,b.bottom);
     }
   }
   return right>left && bottom>top;
 };
 const nodes = (root,selector) => selector ? [...root.querySelectorAll(selector)] : [];
 const shown = (root,selector) => nodes(root,selector).filter(visible);
 const boundedText = (e,limit) => {
   if(e.matches('input,textarea,script,style,iframe') ||
      e.querySelector('input,textarea,script,style,iframe')) throw Error('unsafe_field');
   // Only a field node, never the whole card/body. Do not read hidden child text.
   const walker=document.createTreeWalker(e,NodeFilter.SHOW_TEXT);
   let result='', n;
   while(n=walker.nextNode()) {
     if(visible(n.parentElement)) result+=n.textContent+' ';
     if(result.length>limit) throw Error('oversized_field');
   }
   return result.replace(/\s+/g,' ').trim();
 };
 const attribute = (e,name) => {
   if(!e||!name) return null;
   const v=e.getAttribute(name);
   if(v && v.length>200) throw Error('oversized_identifier');
   return v;
 };
 try {
   if(document.querySelector('input[type=password]')) return fail('authentication_required');
   if(shown(document,'video,audio').length) return fail('unsafe_media_view');
   // Only fixed UI labels are tested, no document/body text dump.
   // During bounded scrolling only fixed UI labels may be retained offscreen.
   // Task/course text below still uses viewport visibility.
   const controls=(root,s)=>p.capture_fragments?nodes(root,s).filter(rendered):shown(root,s);
   const controlText=e=>p.capture_fragments?(e.innerText.length<=20?e.innerText.trim():''):boundedText(e,20);
   const tabs=controls(document,'.ant-tabs-tab[aria-selected="true"]');
   if(tabs.length!==1||controlText(tabs[0])!=='作业') return fail('wrong_task_tab');
   const panes=controls(document,'.ant-tabs-tabpane-active');
   if(panes.length!==1) return fail('active_pane_changed');
   const pane=panes[0];
   const states=controls(pane,'.state-tabs .menu-pane.active');
   if(states.length!==1||controlText(states[0])!=='未完成') return fail('wrong_status_filter');
   // A legitimate empty list can have zero height. Count offscreen card nodes,
   // but never read their fields; CSS-hidden alternate lists remain excluded.
   const lists=nodes(pane,'.homework-content').filter(rendered);
   if(lists.length!==1) return fail('task_list_changed');
   const list=lists[0];
   const scope=list.closest(p.course_scope);
   if(!scope || scope===document.body || scope===document.documentElement ||
      nodes(scope,'.homework-content').length!==1) return fail('ambiguous_course_scope');
   const courses=shown(scope,p.course);
   if(courses.some(c=>c.closest('.homework-item')||list.contains(c)))
     return fail('course_context_inside_task_list');
   const course=courses.length===1 ? boundedText(courses[0],100) : null;
   const all=nodes(list,'.homework-item');
   if(all.length>1000) return fail('loaded_node_limit');
   if(all.some(c=>c.querySelector('.homework-item'))) return fail('nested_task_cards');
   const onscreen=all.filter(visible);
   const totals=shown(scope,p.total);
   const totalText=totals.length===1 ? boundedText(totals[0],40) : null;
   const next=shown(scope,p.next);
   if(next.length>1 || totals.length>1) return fail('ambiguous_pagination');
   const hasNext=next.length===1 ? !(next[0].matches(':disabled')||
     next[0].getAttribute('aria-disabled')==='true') : null;
   const flags={loading:shown(scope,p.loading).length>0 ||
       [scope,pane,list].some(e=>e.getAttribute('aria-busy')==='true'),
     lazy:shown(scope,p.lazy).length>0,virtual:shown(scope,p.virtual).length>0};
   // Count before reading any task text. Never silently truncate 21+ visible cards.
   const counts={loaded:all.length,visible:onscreen.length,
     outside_view:all.length-onscreen.length,total_text:totalText,has_next:hasNext,...flags};
   if(onscreen.length>p.limit) return {error:'visible_task_limit',counts};
   const positions=onscreen.map(card=>{
     if(!p.scan_position_attribute)return all.indexOf(card)+1;
     const value=attribute(card,p.scan_position_attribute);
     if(!value||!/^\d{1,6}$/.test(value))throw Error('invalid_scan_position');
     const position=Number(value)+1-p.scan_position_base;
     if(position<1)throw Error('invalid_scan_position');
     return position;
   });
   if(new Set(positions).size!==positions.length)return fail('ambiguous_scan_positions');
   if(p.capture_fragments && new Set([...(p.seen_positions||[]),...positions]).size>p.limit)
     return {error:'cumulative_task_limit',counts};
   const empty=shown(list,p.empty).length===1;
   const courseId=attribute(scope,p.course_id_attribute);
   const enrollmentId=attribute(scope,p.enrollment_id_attribute);
   const tasks=[];
   for(const [position,card] of onscreen.entries()) {
     const fields={};
     let error=null;
     for(const [name,selector] of Object.entries({title:p.title,deadline:p.deadline,
         course:p.card_course,source_url:p.link})) {
       const matches=shown(card,selector).filter(n=>n.closest('.homework-item')===card);
       if(matches.length>1) {error='ambiguous_'+name;break;}
       fields[name]=matches[0]||null;
     }
     const present=Object.values(fields).filter(Boolean);
     const texts=[fields.title,fields.deadline,fields.course].filter(Boolean);
     if(!error && (present.some(a=>a===card)||texts.some((a,i)=>texts.some((b,j)=>
         i!==j && (a===b||a.contains(b)))))) error='overlapping_task_fields';
     if(fields.deadline && !fields.deadline.closest('.homework-item-time'))
       error='deadline_outside_verified_container';
     if(error) {tasks.push({position,error});continue;}
     try {
       tasks.push({position,scan_position:p.capture_fragments?positions[position]:null,
         title:fields.title?boundedText(fields.title,200):null,
         deadline:fields.deadline?boundedText(fields.deadline,80):null,
         course:fields.course?boundedText(fields.course,100):null,
         source_url:fields.source_url?fields.source_url.getAttribute('href'):null,
         task_id:attribute(card,p.task_id_attribute)});
     } catch(_) {tasks.push({position,error:'invalid_task_field'});}
   }
   return {counts,empty,course,course_id:courseId,enrollment_id:enrollmentId,tasks,
      observed_positions:p.capture_fragments?positions:[]};
 } catch(_) {return fail('invalid_layout_or_field');}
}"""
