// SPDX-License-Identifier: GPL-3.0-or-later
// test_coordination.js - Test the coordination hub and recipe engine

const { RecipeEngine, BUILTIN_RECIPES } = require('./core/recipe-engine.js');
const { CoordinationHub, ProviderManager, A2AProtocol, MemoryOptimizer, SharedMemory, DEFAULT_PROVIDER_CONFIG } = require('./core/coordination-hub.js');

async function testRecipeEngine() {
  console.log('\n=== Testing Recipe Engine ===\n');

  // Mock bridge for testing
  const mockBridge = {
    async callTool(name, args, server) {
      console.log(`  [MockBridge] callTool: ${name}`, args);
      if (name === 'git_diff') return { text: 'mock diff output' };
      if (name === 'git_status') return { files: [] };
      return { ok: true, result: `mock result for ${name}` };
    }
  };

  const engine = new RecipeEngine(mockBridge);

  // Register builtin recipes
  for (const [name, recipe] of Object.entries(BUILTIN_RECIPES)) {
    engine.registerRecipe(name, recipe);
  }

  console.log('Registered recipes:', engine.listRecipes());

  // Test code-review recipe
  console.log('\n--- Running code-review recipe ---');
  try {
    const result = await engine.executeRecipe('code-review', { repoPath: '.', baseBranch: 'main' });
    console.log('Result:', result.status, '- steps:', Object.keys(result.stepResults).length);
  } catch (e) {
    console.log('Error (expected - mock bridge):', e.message);
  }

  // Test custom recipe
  console.log('\n--- Running custom recipe ---');
  engine.registerRecipe('test-custom', {
    name: 'test-custom',
    description: 'Test custom recipe with multiple step types',
    variables: { input: 'hello' },
    steps: [
      { id: 'log1', type: 'log', args: { message: 'Starting with input: ${input}' } },
      { id: 'set_var', type: 'set', args: { name: 'computed', value: 'world' } },
      { id: 'log2', type: 'log', args: { message: 'Computed: ${computed}' } },
      { id: 'wait', type: 'wait', args: { ms: 100 } },
      { id: 'transform', type: 'transform', args: { from: 'input', operation: 'map', mapper: { upper: '${item.toUpperCase()}' } }, output: { mapped: 'data' } },
      { id: 'branch', type: 'branch', args: { condition: 'mapped.length > 0' } },
      { id: 'parallel', type: 'parallel', args: { steps: [ { type: 'log', args: { message: 'Parallel 1' } }, { type: 'log', args: { message: 'Parallel 2' } } ] } },
      { id: 'loop', type: 'loop', args: { items: ['a', 'b', 'c'], step: { type: 'log', args: { message: 'Loop item: ${loopItem}' } } } }
    ]
  });

  try {
    const result = await engine.executeRecipe('test-custom', { input: 'test' });
    console.log('Result:', result.status, '- steps:', Object.keys(result.stepResults).length);
    console.log('Variables:', result.variables);
  } catch (e) {
    console.log('Error:', e.message);
  }
}

async function testProviderManager() {
  console.log('\n=== Testing Provider Manager ===\n');

  const pm = new ProviderManager();

  // Register test providers (mock)
  pm.registerProvider('test-provider-1', {
    endpoint: 'https://api.example.com/v1/chat/completions',
    apiKey: 'test-key-1',
    models: ['model-a', 'model-b'],
    requestsPerMinute: 100,
    tokensPerMinute: 50000,
    maxConcurrent: 3
  });

  pm.registerProvider('test-provider-2', {
    endpoint: 'https://api.example.com/v1/chat/completions',
    apiKey: 'test-key-2',
    models: ['model-c'],
    requestsPerMinute: 50,
    tokensPerMinute: 30000,
    maxConcurrent: 2
  });

  console.log('Providers registered');

  // Test provider selection
  const best = pm.getBestProvider({ model: 'model-a' });
  console.log('Best provider for model-a:', best);

  const best2 = pm.getBestProvider({ model: 'model-c' });
  console.log('Best provider for model-c:', best2);

  // Test quota tracking
  for (let i = 0; i < 5; i++) {
    pm.usage.get('test-provider-1').requests++;
    pm.usage.get('test-provider-1').tokens += 1000;
  }
  console.log('Usage after 5 requests:', pm.usage.get('test-provider-1'));

  // Test status
  console.log('Status:', pm.getStatus());
}

async function testA2AProtocol() {
  console.log('\n=== Testing A2A Protocol ===\n');

  const pm = new ProviderManager();
  pm.registerProvider('test', {
    endpoint: 'https://api.example.com/v1/chat/completions',
    apiKey: 'test',
    models: ['test-model'],
    requestsPerMinute: 100
  });

  const a2a = new A2AProtocol(pm);

  // Register agents
  a2a.registerAgent('planner', ['planning', 'reasoning'], null);
  a2a.registerAgent('coder', ['coding', 'debugging'], null);
  a2a.registerAgent('reviewer', ['review', 'analysis'], null);

  // Register handler for planner
  a2a.onMessage('plan', async (payload) => {
    return { ok: true, plan: `Plan for: ${payload.task}`, steps: ['step1', 'step2', 'step3'] };
  });

  // Test local message
  console.log('--- Local message test ---');
  const result = await a2a.sendMessage('planner', { type: 'plan', payload: { task: 'build a web app' } });
  console.log('Planner response:', result);

  // Test broadcast
  console.log('\n--- Broadcast test ---');
  const broadcastResults = await a2a.broadcastToCapability('coding', { type: 'ping' });
  console.log('Broadcast results:', broadcastResults);

  console.log('\nAgents:', a2a.getAgents());
}

async function testMemoryOptimizer() {
  console.log('\n=== Testing Memory Optimizer ===\n');

  const pm = new ProviderManager();
  pm.registerProvider('test', { endpoint: 'x', apiKey: 'x', models: ['x'], requestsPerMinute: 100 });

  const memory = new MemoryOptimizer(pm, {
    maxHistoryPerProvider: 10,
    maxTotalTokens: 1000,
    compressionThreshold: 0.5
  });

  // Add many messages
  for (let i = 0; i < 15; i++) {
    memory.addMessage('test', {
      role: i % 2 === 0 ? 'user' : 'assistant',
      content: `Message ${i} with some content to make it longer and test token counting`
    });
  }

  console.log('Stats after 15 messages:', memory.getStats());
  console.log('History length:', memory.getHistory('test').length);

  // Test token-limited retrieval
  const limited = memory.getHistory('test', 200);
  console.log('History with 200 token limit:', limited.length, 'messages');
}

async function testCoordinationHub() {
  console.log('\n=== Testing Coordination Hub ===\n');

  const hub = new CoordinationHub({
    providers: {
      'mock-openai': {
        endpoint: 'https://api.openai.com/v1/chat/completions',
        apiKey: 'mock-key',
        models: ['gpt-4', 'gpt-3.5-turbo'],
        requestsPerMinute: 60,
        tokensPerMinute: 150000
      },
      'mock-deepseek': {
        endpoint: 'https://api.deepseek.com/v1/chat/completions',
        apiKey: 'mock-key',
        models: ['deepseek-chat'],
        requestsPerMinute: 60,
        tokensPerMinute: 200000
      }
    },
    healthCheckInterval: 0, // Disable for test
    memoryCheckInterval: 0
  });

  console.log('Hub created with providers:', hub.providerManager.providers.size);

  // Register agents
  hub.registerAgent('orchestrator', ['orchestration', 'planning'], null);
  hub.registerAgent('worker-1', ['coding', 'testing'], null);
  hub.registerAgent('worker-2', ['coding', 'documentation'], null);

  console.log('Agents registered:', hub.a2a.getAgents().length);

  // Test status
  const status = hub.getStatus();
  console.log('Status:', JSON.stringify(status, null, 2).slice(0, 500) + '...');

  // Cleanup
  hub.shutdown();
}

async function testSharedMemory() {
  console.log('\n=== Testing Shared Memory ===\n');

  const fs = require('fs');
  const os = require('os');
  const path = require('path');
  const statePath = path.join(os.tmpdir(), 'sm_js_test.json');
  if (fs.existsSync(statePath)) fs.unlinkSync(statePath);

  const assert = [];
  const check = (label, cond) => {
    assert.push({ label, cond });
    console.log(`${cond ? 'PASS' : 'FAIL'} ${label}`);
  };

  const sm = new SharedMemory({ statePath, shortLimit: 5, longLimit: 5 });
  sm.record('task-a', [{ role: 'user', content: 'build X' }]);
  sm.record('task-a', [{ role: 'assistant', content: 'building X' }]);
  check('short-term holds messages', sm.getShortTerm('task-a').length === 2);
  check('lastTask set', sm.lastTask === 'task-a');

  for (let i = 0; i < 6; i++) {
    sm.record('task-a', [{ role: 'user', content: `step ${i}` }]);
  }
  check('short-term capped', sm.getShortTerm('task-a').length <= 5);
  check('long-term promoted', sm.getLongTerm('task-a').length >= 1);

  sm.record('task-b', [{ role: 'user', content: 'other' }]);
  const boot = sm.bootstrapMessages('task-a');
  const joined = boot.map(m => m.content || '').join(' ');
  check('last-work redirect emitted', joined.includes('task-b'));
  check('bootstrap returns messages', Array.isArray(boot));

  const sm2 = new SharedMemory({ statePath });
  check('persisted round-trip', sm2.getShortTerm('task-a').length >= 1);

  // prepare(): inject + record (fresh task so length is deterministic)
  const merged = sm2.prepare('task-c', [{ role: 'user', content: 'next' }]);
  check('prepare injects and records', sm2.getShortTerm('task-c').length === 1 && merged.length >= 1);

  fs.unlinkSync(statePath);
  return assert;
}

async function testSharedMemoryPrepareRequest() {
  console.log('\n=== Testing Shared Memory via Hub ===\n');

  const assert = [];
  const check = (label, cond) => {
    assert.push({ label, cond });
    console.log(`${cond ? 'PASS' : 'FAIL'} ${label}`);
  };

  const hub = new CoordinationHub({
    providers: {
      'mock-openai': {
        endpoint: 'https://api.openai.com/v1/chat/completions',
        apiKey: 'mock-key',
        models: ['gpt-4'],
        requestsPerMinute: 60,
        tokensPerMinute: 150000
      },
      'mock-deepseek': {
        endpoint: 'https://api.deepseek.com/v1/chat/completions',
        apiKey: 'mock-key',
        models: ['deepseek-chat'],
        requestsPerMinute: 60,
        tokensPerMinute: 200000
      }
    },
    healthCheckInterval: 0,
    memoryCheckInterval: 0
  });

  const res = hub.prepareRequest('mock-openai', [{ role: 'user', content: 'continue work' }], { task: 'feat-42' });
  check('prepare returns task', res.task === 'feat-42');
  check('first request passes through', res.messages.length >= 1);
  check('status exposes shared', !!hub.getStatus().shared);

  // second call on same task shares the first call's context
  const res2 = hub.prepareRequest('mock-deepseek', [{ role: 'user', content: 'next' }], { task: 'feat-42' });
  const joined = res2.messages.map(m => m.content || '').join(' ');
  check('second provider same task', res2.task === 'feat-42');
  check('shared memory injected', joined.includes('[shared-memory'));
  check('short-term accumulates across providers', hub.shared.stats().shortMessages >= 2);

  // last-work redirect when booting a different task
  const res3 = hub.prepareRequest('mock-openai', [{ role: 'user', content: 'fresh' }], { task: 'new-task' });
  const bootJoined = res3.messages.map(m => m.content || '').join(' ');
  check('last-work redirect to previous task', bootJoined.includes('feat-42'));

  hub.shutdown();
  return assert;
}

async function runAllTests() {
  console.log('Starting coordination hub tests...\n');

  await testRecipeEngine();
  await testProviderManager();
  await testA2AProtocol();
  await testMemoryOptimizer();
  await testSharedMemory();
  await testSharedMemoryPrepareRequest();
  await testCoordinationHub();

  console.log('\n=== All Tests Completed ===');
}

runAllTests().catch(console.error);