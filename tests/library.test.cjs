const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');

test('same text from local and companion is one entry, different revisions remain',()=>{
  const context=vm.createContext({$:()=>({})});
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../pages/我只在乎你/library.js'),'utf8'),context);
  const result=vm.runInContext(`uniqueLibrary([
    {id:'remote',kind:'text',title:'Story',content_hash:'same',producer:'private_companion'},
    {id:'local',kind:'text',title:'Story',content_hash:'same',path:'story.txt'},
    {id:'revision',kind:'text',title:'Story',content_hash:'different'}
  ])`,context);
  assert.equal(result.length,2);
  assert.equal(result[0].id,'local');
  assert.equal(result[0].companion_copy,true);
  assert.equal(result[0].aliases.includes('remote'),true);
});
