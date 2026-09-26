# Validação PostgreSQL → BigQuery com saída Parquet

O `validate_migration.py` compara cada tabela/view do PostgreSQL com a tabela
correspondente no BigQuery. O resultado é gravado em Parquet e pode ser anexado
automaticamente a uma tabela do BigQuery para uso no Looker ou Looker Studio.

## Quero executar apenas a validação

Siga o [guia da VM](guia-vm.md), especialmente os passos 4 a 6. Ele mostra como
configurar e executar `validate_migration.py` com relatório somente local, sem
rodar a migração. Também explica como instalar o Git e atualizar o projeto.

## O que é validado

- quantidade de registros na origem e no destino;
- diferença com sinal e percentual;
- quantidade, nomes e tipos esperados das colunas;
- tabela de destino inexistente;
- erro individual por objeto, sem perder o restante do relatório;
- histórico por `execution_id` e `validated_at`.

O status final pode ser `OK`, `ROW_MISMATCH`, `SCHEMA_MISMATCH`,
`ROW_AND_SCHEMA_MISMATCH`, `DESTINATION_NOT_FOUND`, `ESTIMATE_MATCH` ou `ERROR`.

> O Looker não lê o arquivo local da VM. A rota recomendada é o script gerar o
> Parquet e carregá-lo em uma tabela de monitoramento no BigQuery; essa tabela é
> então conectada ao Looker.

## 1. Instalação

Use Python 3.10 ou superior e o ambiente virtual atual da migração:

```bash
cd /opt/migracao

/opt/migracao/.venv/bin/python -m pip install \
  -r /opt/migracao/requirements-validation.txt
```

Copie o exemplo e ajuste sem remover as configurações existentes:

```bash
if ! sudo test -f /opt/migracao/.env.validacao; then
  sudo install -o root -g postgres -m 640 \
    /opt/migracao/config/examples/.env.validacao.example /opt/migracao/.env.validacao
fi
```

Campos essenciais:

```env
PG_HOST=/var/run/postgresql
PG_PORT=5437
PG_USER=postgres
PG_PASSWORD=
PG_ADMIN_DATABASE=postgres

BQ_PROJECT=seu-projeto-gcp
BQ_LOCATION=southamerica-east1
VALIDATION_OUTPUT_DIR=/opt/migracao/validation
VALIDATION_BQ_TABLE=seu-projeto-gcp.monitoramento.validacao_migracao
```

`seu-projeto-gcp` acima é só exemplo: troque pelo **Project ID** real do GCP
(não o nome de exibição) em `BQ_PROJECT` e em `VALIDATION_BQ_TABLE`, incluindo se
passar `--bq-report-table` na linha de comando. Deixar o valor de exemplo causa
`404 Project ... is not found` ao tentar publicar no BigQuery — o script recusa
esse valor antes de tentar (`ValueError`), mas mais cedo é melhor que mais tarde.
Confira com `grep BQ_PROJECT /opt/migracao/.env.validacao` se estiver em dúvida.

Se a autenticação do PostgreSQL exigir senha, preencha `PG_PASSWORD`. O script
não imprime a senha nos logs.

## 2. Teste controlado

Valide primeiro um banco, um schema e uma tabela:

```bash
sudo -u postgres env -u GOOGLE_APPLICATION_CREDENTIALS \
  /opt/migracao/.venv/bin/python \
  /opt/migracao/validate_migration.py \
  --env-file /opt/migracao/.env.validacao \
  --databases 23070001-99-SV \
  --schemas gold \
  --tables 99_RJ \
  --output /opt/migracao/validation/teste.parquet \
  --verbose
```

Confira o arquivo:

```bash
/opt/migracao/.venv/bin/python -c "
import pandas as pd
df = pd.read_parquet('/opt/migracao/validation/teste.parquet')
print(df[['source_database','source_schema','source_table','source_rows','bq_rows','validation_status']].to_string(index=False))
"
```

## 3. Execução completa e publicação para o Looker

Quando `VALIDATION_BQ_TABLE` estiver preenchida, o script cria o Parquet e faz
`append` das linhas na tabela de histórico:

```bash
sudo -u postgres env -u GOOGLE_APPLICATION_CREDENTIALS \
  /opt/migracao/.venv/bin/python \
  /opt/migracao/validate_migration.py \
  --env-file /opt/migracao/.env.validacao \
  --fail-on-difference
```

O código de saída será `2` se alguma tabela divergir. Sem
`--fail-on-difference`, o relatório é produzido e o processo retorna `0`, mesmo
que o Parquet contenha divergências por tabela.

Para validar somente contagens aproximadas, mais rapidamente:

```bash
... validate_migration.py --count-mode metadata
```

Use `exact` para a validação final. Em `metadata`, o PostgreSQL usa estatísticas
do catálogo e o BigQuery usa metadados, que podem estar defasados.

## 4. Execução em background

```bash
sudo install -d -o postgres -g postgres -m 750 \
  /opt/migracao/logs /opt/migracao/validation

sudo -u postgres bash -c '
  cd /opt/migracao &&
  nohup env -u GOOGLE_APPLICATION_CREDENTIALS PYTHONUNBUFFERED=1 \
    /opt/migracao/.venv/bin/python \
    /opt/migracao/validate_migration.py \
    --env-file /opt/migracao/.env.validacao \
    >> /opt/migracao/logs/validacao.log 2>&1 < /dev/null &
  echo $! > /opt/migracao/logs/validacao.pid
'
```

Acompanhe:

```bash
tail -f /opt/migracao/logs/validacao.log
ps -fp "$(cat /opt/migracao/logs/validacao.pid)"
```

## 5. Fonte de dados no Looker

Use `seu-projeto.monitoramento.validacao_migracao` como fonte. Dimensões e
métricas sugeridas:

- data: `validated_at`;
- execução: `execution_id`;
- dimensões: banco, schema, tabela e `validation_status`;
- métricas: `source_rows`, `bq_rows`, `row_difference` e
  `row_difference_pct`;
- cartão: percentual de tabelas com `is_valid = true`;
- filtro: última execução por `validated_at`.

A Service Account da VM precisa consultar as tabelas migradas e gravar na
tabela de monitoramento. Se o dataset `monitoramento` ainda não existir, ela
também precisará de `bigquery.datasets.create` no projeto, incluída em
`roles/bigquery.user`.

## 6. Mapeamentos excepcionais

O padrão automático é:

```text
dataset = banco normalizado
tabela  = schema_tabela
```

Hífens e outros caracteres são trocados por `_`. Se o destino real usar outro
nome, copie `config/examples/validation-mapping.example.json`, ajuste-o e configure:

```env
VALIDATION_MAPPING_FILE=/opt/migracao/validation-mapping.json
```

## 7. Colunas principais do Parquet

| Coluna | Uso |
| --- | --- |
| `execution_id` | identifica uma rodada completa |
| `validated_at` | data/hora UTC da rodada |
| `source_database`, `source_schema`, `source_table` | objeto de origem |
| `bq_project`, `bq_dataset`, `bq_table` | objeto de destino |
| `source_rows`, `bq_rows` | contagens comparadas |
| `row_difference`, `row_difference_pct` | divergência |
| `missing_columns_json`, `extra_columns_json` | diferenças de colunas |
| `validation_status`, `is_valid` | situação para painéis e alertas |
| `error_message` | diagnóstico limitado a 4.000 caracteres |

## 8. Observações operacionais

- O modo `exact` pode consumir tempo e recursos em tabelas grandes.
- A saída usa compressão Snappy, compatível com BigQuery e ferramentas Parquet.
- Cada execução gera um novo arquivo e novas linhas no histórico do BigQuery.
- Não rode duas validações completas simultaneamente na mesma VM.
- A autenticação GCP usa a Service Account da VM; não é necessário arquivo de chave.

## Melhorias e interpretação do relatório

- Copie os três módulos: `validate_migration.py`, `migrate.py` e
  `migration_common.py`. O validador reutiliza as regras de tipos e nomes da migração.
- Falhas de conexão/inventário geram uma linha `ERROR` com
  `source_object_type=DATABASE`; os demais bancos continuam sendo validados.
- Tabelas pedidas em `--tables` que não forem encontradas geram erro de inventário,
  em vez de desaparecerem silenciosamente do relatório daquele banco.
- A descoberta inclui views materializadas, partições e tabelas externas. A
  tabela particionada pai já contém as linhas de todas as partições; defina
  `PG_SKIP_PARTITION_CHILDREN=true` para validar somente o pai e não contar
  cada partição filha como um objeto separado.
- `PG_DATABASES`, `PG_SCHEMAS` e os filtros `PG_EXCLUDE_*` também são considerados.
  Argumentos de bancos/schemas na linha de comando substituem suas listas de inclusão.
- Um destino ausente é registrado sem executar `COUNT(*)` na origem. Nesse caso,
  contagens e quantidades de colunas ficam nulas, não zero.
- Tipos divergentes aparecem em `error_message` e causam `SCHEMA_MISMATCH`.
  Migrações antigas podem divergir das novas regras de `numeric`; revise antes
  de recarregar. Os campos existentes do relatório foram preservados.
- `metadata` nunca comprova igualdade: contagens estimadas iguais recebem
  `ESTIMATE_MATCH`, com `is_valid=false`. Views, tabelas particionadas, tabelas
  externas, pais com herança e estatísticas ausentes exigem `exact`.
- `row_difference` é a diferença **com sinal** (`destino - origem`);
  use seu valor absoluto no painel se desejar.
- Contagem, nomes e tipos iguais não comprovam conteúdo idêntico. Alterações na
  origem durante ou depois da migração podem gerar diferenças legítimas.
- O Parquet é escrito atomicamente e mantém tipos numéricos estáveis mesmo se
  todas as tabelas estiverem ausentes. Contagens grandes não passam por float.
- Aliases manuais (`PG_TABLES=origem:alias`) e `BQ_DATASET` precisam ser refletidos
  no JSON de mapping; o validador usa o mapeamento automático por padrão.
- O append à tabela de histórico do BigQuery (`VALIDATION_BQ_TABLE`) permite adição
  automática de coluna, para que uma futura coluna nova no relatório não quebre o
  append das execuções seguintes contra o histórico já existente.

Para executar apenas a comparação local, deixe `VALIDATION_BQ_TABLE` e
`VALIDATION_GCS_URI` vazios. Para operação automatizada, use sempre
`--fail-on-difference`; sem essa opção divergências permanecem somente no relatório.
O Parquet local é preservado se o envio ao GCS ou ao BigQuery falhar.

Teste após atualizar os arquivos:

```bash
cd /opt/migracao
/opt/migracao/.venv/bin/python -m unittest discover -v
sudo -u postgres /opt/migracao/.venv/bin/python /opt/migracao/migrate.py --inventory
sudo -u postgres /opt/migracao/.venv/bin/python /opt/migracao/validate_migration.py \
  --env-file /opt/migracao/.env.validacao --count-mode exact --fail-on-difference
```
