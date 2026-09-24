const boxes=[...document.querySelectorAll('input[name="draft_id"]:not(:disabled)')];
const all=document.getElementById('select-page');
const count=document.getElementById('selection-count');
const form=document.getElementById('bulk-form');
function update(){const n=boxes.filter(b=>b.checked).length;if(count)count.textContent=`${n} selected`;if(all){all.checked=boxes.length>0&&n===boxes.length;all.indeterminate=n>0&&n<boxes.length;}if(form)form.querySelector('button').disabled=n===0;}
if(all)all.addEventListener('change',()=>{boxes.forEach(b=>b.checked=all.checked);update();});
boxes.forEach(b=>b.addEventListener('change',update));update();
