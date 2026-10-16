# Cálculo inspirado em amplitudes quânticas — proposta para aprovação

Status: **implementado como diagnóstico experimental**, autorizado pelo usuário. Usa os últimos 20 candles M1 para apoiar Jev, sem fabricar confiança ou garantir lucro.

## Objetivo

Enviar ao JEV uma descrição matemática dos candles reais, sem ATR, médias
móveis, RSI, MACD ou sinais de entrada convencionais. JEV continua escolhendo
BUY/SELL/HOLD/AVOID; o controle de risco continua podendo recusar a entrada.

## Cálculo proposto

Para os fechamentos consecutivos dos últimos 20 candles M1 encerrados:

1. Calcular o deslocamento relativo `r_i = log(close_i / close_(i-1))`.
2. Separar as observações em alta, baixa ou estabilidade. A tolerância de
   estabilidade é o menor passo de preço do contrato, dividido pelo preço.
3. Usar volume de ticks como peso relativo `w_i`, sem confundi-lo com volume
   financeiro. Se não houver volume válido suficiente, não calcular o diagnóstico.
4. Definir a fase `theta_i = (pi/2) * r_i / max(abs(r))`. Se todos os preços
   forem iguais, usar fase zero e somente o estado estável.
5. Somar amplitudes complexas por grupo:
   `A_k = sum(sqrt(w_i) * exp(j * theta_i))`.
6. Normalizar: `psi_k = A_k / sqrt(sum(abs(A_k)^2))`.
7. Produzir `peso_k = abs(psi_k)^2`, cuja soma é um.

Também produzir coerência de fase
`abs(sum(sqrt(w_i)*exp(j*theta_i)))^2 / sum(sqrt(w_i))^2`
e entropia normalizada dos três pesos.

Esses valores são **pesos descritivos do histórico**, não probabilidades de
acerto, previsão de preço ou confiança operacional. Não serão usados para
inventar a probabilidade/confiança que o JEV precisa retornar.

O mapeamento de preços/volume para fases e amplitudes acima é uma hipótese
local de modelagem, ainda sem validação financeira. Não reivindica novidade
acadêmica, vantagem de mercado ou equivalência física com um mercado quântico.
Não exige Qiskit, computador quântico, nova API ou infraestrutura externa.

## Fonte da inspiração

A documentação da IBM descreve estados como vetores normalizados de amplitudes
complexas, fases e pesos obtidos pelo módulo ao quadrado:

- [Estados e regra de Born](https://quantum.cloud.ibm.com/learning/en/courses/basics-of-quantum-information/single-systems/quantum-information)
- [Fases e interferência](https://quantum.cloud.ibm.com/learning/en/courses/use-a-qc-today/quantum-mechanics-basics)

Essas fontes explicam matemática quântica; não validam o uso proposto no mercado.

## Implementação e impactos, se aprovada

- Um módulo puro, determinístico, recebe somente candles reais e o passo de preço.
- O resultado entra em `MarketState.mathematics` e no payload do JEV.
- Nenhum BUY/SELL automático, nenhuma mudança nos percentuais de risco e nenhuma
  ordem real. O cálculo é diagnóstico e não substitui os limites paper.
- Exibir janela, quantidade de observações, pesos, coerência, entropia e origem.
- Testar normalização, extremos de alta/baixa/estabilidade, dados insuficientes,
  invariância de escala do preço e pesos, NaN, volume ausente e determinismo.
- Qualquer alegação de utilidade preditiva exigirá validação temporal fora da
  amostra com custos reais; até lá, o resultado será identificado como experimental.

Implementação autorizada pelo pedido de cálculos quantitativos e inspirados em amplitudes.
