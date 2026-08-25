// Pico de 2.000 requests simultaneos, que e o cenario de referencia da arquitetura.
//
// Valida a meta de p99 abaixo de 50 ms fora cold start, e serve para descobrir a cota de
// concorrencia da Lambda antes de descobrir em producao. Contra a API local o numero mede
// o caminho de leitura sem I/O; contra a Function URL, mede tambem a rede e o cold start.
//
//   k6 run tests/load/get_pool.js
//   k6 run -e BASE_URL=https://<function-url> tests/load/get_pool.js

import http from 'k6/http';
import { check } from 'k6';
import { Rate } from 'k6/metrics';

const BASE_URL = __ENV.BASE_URL || 'http://localhost:8000';

const degraded = new Rate('respostas_degradadas');
const stale = new Rate('respostas_com_snapshot_velho');

export const options = {
  scenarios: {
    // Baseline baixo com pico subito: e o padrao que o enunciado descreve, times
    // disparando jobs de forma descentralizada.
    baseline: {
      executor: 'constant-arrival-rate',
      rate: 20, timeUnit: '1s', duration: '1m',
      preAllocatedVUs: 20, maxVUs: 100,
    },
    pico: {
      executor: 'ramping-arrival-rate',
      startTime: '30s', startRate: 50, timeUnit: '1s',
      preAllocatedVUs: 200, maxVUs: 2000,
      stages: [
        { target: 2000, duration: '10s' },
        { target: 2000, duration: '30s' },
        { target: 0, duration: '10s' },
      ],
    },
  },
  thresholds: {
    'http_req_duration{scenario:baseline}': ['p(99)<50'],
    'http_req_duration{scenario:pico}': ['p(99)<200'],
    http_req_failed: ['rate<0.001'],
    respostas_degradadas: ['rate<0.01'],
  },
};

const FILTROS = [
  '?job_id=etl-vendas&profile=memory',
  '?job_id=etl-pesado&family=r6',
  '?job_id=agg-diario&instance_types=r6.xlarge,r6.2xlarge',
  '?profile=compute',
  '?job_id=ml-features&availability_zones=us-east-1a,us-east-1b',
  '',
];

export default function () {
  const filtro = FILTROS[Math.floor(Math.random() * FILTROS.length)];
  const res = http.get(`${BASE_URL}/get-pool${filtro}`, { tags: { name: 'get-pool' } });

  const ok = check(res, {
    'status 200': (r) => r.status === 200,
    'devolveu um pool': (r) => r.status === 200 && String(r.json('pool_id') || '').startsWith('pool-'),
    'score valido': (r) => {
      if (r.status !== 200) return false;
      const score = r.json('score');
      return typeof score === 'number' && score >= 0 && score <= 1;
    },
  });

  if (ok && res.status === 200) {
    degraded.add(res.json('degraded') === true);
    stale.add(res.json('snapshot.stale') === true);
  }
}
