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
