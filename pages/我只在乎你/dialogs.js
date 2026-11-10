"use strict";
let activePageDialogs=0;
function pageDialog(message, initial, inputMode, choices=null) {
  if(activePageDialogs)return Promise.resolve(inputMode?null:false);
  activePageDialogs++;
  return new Promise(resolve=>{
    const overlay=document.createElement("div");
    overlay.className="dialog-overlay";
    const box=document.createElement("section");box.className="panel dialog-box";
    box.setAttribute("role","dialog");box.setAttribute("aria-modal","true");box.setAttribute("aria-label",message);
    const text=document.createElement("p");text.textContent=message;box.append(text);
    const input=document.createElement("input");
    if(inputMode){input.value=initial||"";box.append(input);}
    const actions=document.createElement("div");actions.className="actions";
    actions.style.marginTop="14px";
    const cancel=document.createElement("button");cancel.type="button";cancel.textContent="取消";
    const ok=document.createElement("button");ok.type="button";ok.className="btn-primary";ok.textContent="确认";
    const previous=document.activeElement;
    let settled=false;
    function finish(value){if(settled)return;settled=true;overlay.remove();activePageDialogs--;if(previous?.isConnected)previous.focus();resolve(value);}
    cancel.onclick=()=>finish(inputMode?null:false);
    ok.onclick=()=>finish(inputMode?input.value:true);
    actions.append(cancel);
    const targets=inputMode?[input,cancel]:[cancel];
    if(choices){cancel.onclick=()=>finish(null);for(const choice of choices){const b=document.createElement("button");b.type="button";b.textContent=choice.label;b.onclick=()=>finish(choice.value);actions.append(b);targets.push(b);}}
    else{actions.append(ok);targets.push(ok);}
    overlay.onkeydown=event=>{if(event.key==="Escape"){event.preventDefault();cancel.click();}if(event.key==="Enter"&&inputMode){event.preventDefault();ok.click();}if(event.key==="Tab"){const index=targets.indexOf(document.activeElement);event.preventDefault();targets[(index+(event.shiftKey?-1:1)+targets.length)%targets.length].focus();}};
    box.append(actions);overlay.append(box);document.body.append(overlay);(inputMode?input:cancel).focus();
  });
}
const pageConfirm=message=>pageDialog(message,"",false);
const pagePrompt=(message,initial="")=>pageDialog(message,initial,true);
const pageChoose=(message,choices)=>pageDialog(message,"",false,choices);
function pageForm(message,fields,submitLabel="保存") {
  if(activePageDialogs)return Promise.resolve(null);
  activePageDialogs++;
  return new Promise(resolve=>{
    const overlay=document.createElement("div");overlay.className="dialog-overlay";
    const box=document.createElement("section");box.className="panel dialog-box";
    box.setAttribute("role","dialog");box.setAttribute("aria-modal","true");box.setAttribute("aria-label",message);
    const text=document.createElement("p");text.textContent=message;box.append(text);
    const inputs=fields.map(field=>{
      const label=document.createElement("label");label.className="field";
      const caption=document.createElement("span");caption.textContent=field.label||"";
      let input;
      if(Array.isArray(field.options)&&field.options.length){
        input=document.createElement("select");
        const values=field.options.map(option=>String(option.value??option.label??""));
        field.options.forEach((option,index)=>{
          const item=document.createElement("option");
          item.value=values[index];
          item.textContent=option.label??values[index];
          if(values[index]===String(field.value??""))item.selected=true;
          input.append(item);
        });
        // 显式回填，保证 value 一定是选项里的一个
        input.value=values.includes(String(field.value??""))?String(field.value):values[0];
      }else{
        input=document.createElement("input");
        input.value=field.value||"";
        input.maxLength=field.maxLength||600;
        if(field.placeholder)input.placeholder=field.placeholder;
      }
      label.append(caption);label.append(input);box.append(label);return input;
    });
    const actions=document.createElement("div");actions.className="actions";actions.style.marginTop="14px";
    const cancel=document.createElement("button");cancel.type="button";cancel.textContent="取消";
    const ok=document.createElement("button");ok.type="button";ok.className="btn-primary";ok.textContent=submitLabel;
    const previous=document.activeElement;
    let settled=false;
    function finish(value){if(settled)return;settled=true;overlay.remove();activePageDialogs--;if(previous?.isConnected)previous.focus();resolve(value);}
    cancel.onclick=()=>finish(null);
    ok.onclick=()=>finish(inputs.map(input=>input.value));
    actions.append(cancel);actions.append(ok);
    overlay.onkeydown=event=>{
      if(event.key==="Escape"){event.preventDefault();cancel.click();}
      if(event.key==="Enter"){event.preventDefault();ok.click();}
      if(event.key==="Tab"){const targets=[...inputs,cancel,ok];const index=targets.indexOf(document.activeElement);event.preventDefault();targets[(index+(event.shiftKey?-1:1)+targets.length)%targets.length].focus();}
    };
    box.append(actions);overlay.append(box);document.body.append(overlay);(inputs[0]||cancel).focus();
  });
}
