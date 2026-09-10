# Nested regions — implementation plan

> plan p-golden-regions · revision r0001 · projection reviewer/implementation · compiler 1 · graph sha256:a206f09973b20bdb7e12e8db12f6f0effd8d1ead013400dcb9bcf5acaa3c7bb1 · projection sha256:e8c02b006477cedfa8dcd9d64620c0aad178e4f0793f72251054c4d01ed4dc8e

## Tasks

### task-1 · Nested task
meta: {"attrs":{},"body_chars":0,"body_state":"full","created_rev":"r0001","ext":null,"geometry":null,"kind":"task","order":5000,"stage":"implementation","updated_rev":"r0001"}
relationships-json: []



## Discussion

### reg-1 · Region 1
meta: {"attrs":{},"body_chars":0,"body_state":"full","created_rev":"r0001","ext":null,"geometry":{"h":520,"w":700,"x":20,"y":20,"z":1},"kind":"region","order":1000,"stage":"implementation","updated_rev":"r0001"}
contains: reg-2
relationships-json: [{"attrs":{},"created_rev":"r0001","from":"reg-1","id":"edge-1","kind":"contains","to":"reg-2"}]



### reg-2 · Region 2
meta: {"attrs":{},"body_chars":0,"body_state":"full","created_rev":"r0001","ext":null,"geometry":{"h":440,"w":600,"x":40,"y":40,"z":2},"kind":"region","order":2000,"stage":"implementation","updated_rev":"r0001"}
contains: reg-3
relationships-json: [{"attrs":{},"created_rev":"r0001","from":"reg-2","id":"edge-2","kind":"contains","to":"reg-3"}]



### reg-3 · Region 3
meta: {"attrs":{},"body_chars":0,"body_state":"full","created_rev":"r0001","ext":null,"geometry":{"h":360,"w":500,"x":60,"y":60,"z":3},"kind":"region","order":3000,"stage":"implementation","updated_rev":"r0001"}
contains: reg-4
relationships-json: [{"attrs":{},"created_rev":"r0001","from":"reg-3","id":"edge-3","kind":"contains","to":"reg-4"}]



### reg-4 · Region 4
meta: {"attrs":{},"body_chars":0,"body_state":"full","created_rev":"r0001","ext":null,"geometry":{"h":280,"w":400,"x":80,"y":80,"z":4},"kind":"region","order":4000,"stage":"implementation","updated_rev":"r0001"}
contains: task-1
relationships-json: [{"attrs":{},"created_rev":"r0001","from":"reg-4","id":"edge-4","kind":"contains","to":"task-1"}]



## Provenance

- source revision r0001, written unknown by unknown
- graph digest sha256:a206f09973b20bdb7e12e8db12f6f0effd8d1ead013400dcb9bcf5acaa3c7bb1
- projection digest sha256:e8c02b006477cedfa8dcd9d64620c0aad178e4f0793f72251054c4d01ed4dc8e
- compiler grogu-plan-compile/1
provenance-json: {"budget_chars":null,"budget_exceeded":false,"canvas":{"snap":8},"compiler":1,"depth":null,"graph_digest":"sha256:a206f09973b20bdb7e12e8db12f6f0effd8d1ead013400dcb9bcf5acaa3c7bb1","include":"all","plan_id":"p-golden-regions","projection_id":"sha256:cf9a3e58d745117fb5d23f8f83c4486be96cc89908eef0e6e92e62358de43dfc","revision":"r0001","role":"reviewer","sealed_relationships":0,"since":"","source_actor":"","source_agent":"","source_at":"","source_provenance":{},"source_role":"","stages":["implementation"]}
