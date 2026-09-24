const boxes=[...document.querySelectorAll('input[name="draft_id"]:not(:disabled)')];
const count=document.getElementById('selection-count');
const form=document.getElementById('bulk-form');
const bar=document.getElementById('bulk-actions');
const clear=document.getElementById('clear-selection');
function update(){
 const n=boxes.filter(b=>b.checked).length;
 if(count)count.textContent=`${n} selected`;
 if(bar)bar.hidden=n===0;
 if(form)form.querySelector('button[type="submit"]').disabled=n===0;
}
boxes.forEach(b=>b.addEventListener('change',update));
if(clear)clear.addEventListener('click',()=>{boxes.forEach(b=>b.checked=false);update();if(boxes[0])boxes[0].focus();});
update();
