const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');

function harness(){
  const elements=[];
  const document={activeElement:null};
  function element(tag){
    const node={tag,style:{},children:[],isConnected:true,
      setAttribute(){},append(...nodes){this.children.push(...nodes);},
      remove(){this.isConnected=false;},focus(){document.activeElement=this;},click(){this.onclick?.();}};
    elements.push(node);return node;
  }
  document.createElement=element;document.body=element('body');
  const context=vm.createContext({document,Promise});
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../pages/我只在乎你/dialogs.js'),'utf8'),context);
  return {context,elements,document,run:code=>vm.runInContext(code,context)};
}

test('embedded confirmation uses page controls, confirm resolves true',async()=>{
  const h=harness();const pending=h.run('pageConfirm("delete?")');
  h.elements.find(e=>e.textContent==='确认').click();
  assert.equal(await pending,true);
  assert.equal(h.run('activePageDialogs'),0);
});
test('choice cancel returns null and never chooses pause-all',async()=>{
  const h=harness();const pending=h.run('pageChoose("pause",[{label:"all",value:"all"}])');
  h.elements.find(e=>e.textContent==='取消').click();
  assert.equal(await pending,null);
});
test('prompt enter returns value and restores focus',async()=>{
  const h=harness();const previous=h.document.createElement('button');previous.focus();
  const pending=h.run('pagePrompt("name","initial")');
  const input=h.elements.find(e=>e.tag==='input');input.value='new project';
  const overlay=h.document.body.children[0];overlay.onkeydown({key:'Enter',preventDefault(){}});
  assert.equal(await pending,'new project');assert.equal(h.document.activeElement,previous);
});
test('second dialog does not stack and escape cancels',async()=>{
  const h=harness();const pending=h.run('pageConfirm("one")');
  assert.equal(await h.run('pageConfirm("two")'),false);
  h.document.body.children[0].onkeydown({key:'Escape',preventDefault(){}});
  assert.equal(await pending,false);assert.equal(h.run('activePageDialogs'),0);
});
test('form field with options renders a select and returns the chosen value',async()=>{
  const h=harness();
  const pending=h.run('pageForm("印象",[{label:"主题",value:"宠物"},{label:"类型",value:"interest",options:[{label:"事实",value:"fact"},{label:"爱好",value:"interest"}]}])');
  const select=h.elements.find(e=>e.tag==='select');
  assert.ok(select,'select rendered for options field');
  assert.equal(select.value,'interest');
  h.document.body.children[0].onkeydown({key:'Enter',preventDefault(){}});
  const values=await pending;
  assert.equal(values.join('|'),'宠物|interest');
});
test('form collects several fields, enter submits and cancel returns null',async()=>{
  const h=harness();
  const pending=h.run('pageForm("偏好",[{label:"主题",value:"语气"},{label:"内容",value:"别用感叹号"}])');
  const inputs=h.elements.filter(e=>e.tag==='input');
  assert.equal(inputs.length,2);
  assert.equal(inputs[0].value,'语气');
  assert.equal(inputs[1].value,'别用感叹号');
  h.document.body.children[0].onkeydown({key:'Enter',preventDefault(){}});
  const values=await pending;
  assert.equal(values.length,2);
  assert.equal(values[0],'语气');
  assert.equal(values[1],'别用感叹号');
  assert.equal(h.run('activePageDialogs'),0);
  const cancelled=h.run('pageForm("偏好",[{label:"主题"}])');
  h.elements.filter(e=>e.textContent==='取消').pop().click();
  assert.equal(await cancelled,null);
  assert.equal(h.run('activePageDialogs'),0);
});
