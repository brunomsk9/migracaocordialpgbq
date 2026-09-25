# PostgreSQL/PostGIS → BigQuery

Migra tabelas ou views do PostgreSQL para o BigQuery, preservando os tipos compatíveis e convertendo colunas PostGIS `geometry`/`geography` para o tipo nativo `GEOGRAPHY`.

## Comece por aqui na VM

Leia o [guia passo a passo para instalar Git, atualizar /opt/migracao e rodar somente a validação](docs/guia-vm.md).
Ele inclui backup da instalação manual, configuração, comandos prontos e leitura
dos resultados. Para conferir tabelas já migradas, execute `validate_migration.py`.

## Organização do projeto

- [Validação e relatório](docs/validacao.md)
- [Estrutura de pastas, commits e distribuição](docs/desenvolvimento.md)
- [Revisão técnica](docs/revisao-2026-09-25.md)

Código executável na raiz, testes em `tests/`, documentação em `docs/` e exemplos
em `config/examples/`. Configurações reais ficam fora do versionamento.

## Comportamento espacial

- Converte geometrias para EPSG:4326 no PostgreSQL.
- Tenta corrigir geometrias inválidas com `ST_MakeValid`.
- Envia WKT ao BigQuery usando schema explícito `GEOGRAPHY`.
- Mantém valores nulos e transforma geometrias vazias em `NULL`.
- Se houver geometria com SRID 0, defina `PG_DEFAULT_SRID` com o SRID real de origem.

## Instalação

Requer Python 3.10 ou superior, acesso ao PostgreSQL/PostGIS e uma conta de serviço com permissão para criar/carregar tabelas no BigQuery.

```bash
cd /opt/migracao
python3 -m venv .venv
source .venv/bin/activate       # Linux/macOS
# .venv\Scripts\activate        # Windows
pip install -r requirements.txt
cp config/examples/.env.example .env
```

Edite `.env`. Na VM que hospeda o PostgreSQL, o modo recomendado descobre todos
os bancos, schemas, tabelas e views automaticamente:

```dotenv
PG_HOST=/var/run/postgresql
PG_USER=postgres
PG_PASSWORD=
AUTO_DISCOVER=true
PG_DATABASES=
PG_SCHEMAS=
```

Execute como o usuário do sistema PostgreSQL para usar autenticação local sem senha:

```bash
sudo -u postgres /opt/migracao/.venv/bin/python /opt/migracao/migrate.py --inventory
sudo -u postgres /opt/migracao/.venv/bin/python /opt/migracao/migrate.py --dry-run
sudo -u postgres /opt/migracao/.venv/bin/python /opt/migracao/migrate.py
```

Garanta que esse usuário consiga ler os arquivos, sem tornar o `.env` público:

```bash
sudo chown -R root:postgres /opt/migracao
sudo chmod 750 /opt/migracao
sudo chmod 640 /opt/migracao/.env
```

O script não executa `sudo` internamente. Ele deve ser iniciado com
`sudo -u postgres` para que a autenticação `peer` do PostgreSQL funcione.

`--inventory` e `--dry-run` não escrevem no BigQuery. A execução normal faz este mapeamento:

```text
PostgreSQL 2304400_fo / dm_analise.tbl_acidentes
BigQuery   2304400_fo / dm_analise_tbl_acidentes
```

Datasets ausentes são criados. Datasets existentes são reutilizados. Bancos
`postgres`, `template0` e `template1`, além dos schemas internos, são ignorados.
É possível limitar a descoberta sem listar tabelas individualmente:

```dotenv
PG_DATABASES=2304400_fo,outro_banco
PG_SCHEMAS=dm_analise,public
PG_EXCLUDE_DATABASES=banco_legado
PG_EXCLUDE_SCHEMAS=topology,tiger
```

Para usar o comportamento manual anterior, configure:

```dotenv
AUTO_DISCOVER=false
PG_DATABASE=2304400_fo
PG_TABLES=dm_analise.*
BQ_DATASET=2304400_fo
```

Também é possível informar uma lista ou substituir manualmente o destino:

```dotenv
PG_TABLES=public.clientes,public.vias:vias_urbanas
```

O destino padrão de `public.clientes` é `public_clientes`; o alias explícito faz
`public.vias` ser gravada como `vias_urbanas`.

## Credencial do Google Cloud

Em uma VM do Google Cloud com Service Account configurada, remova ou comente a variável:

```dotenv
# GOOGLE_APPLICATION_CREDENTIALS não deve ser definida neste caso
```

Fora do Google Cloud, informe o caminho absoluto do JSON da conta de serviço:

```dotenv
GOOGLE_APPLICATION_CREDENTIALS=/caminho/chave.json
```

## Executar

```bash
python migrate.py
```

Opções:

```bash
python migrate.py --tables public.municipios,public.vias --batch-size 10000 --write-mode truncate
```

- `truncate`: substitui a tabela de destino somente após concluir a preparação (padrão).
- `append`: mantém os dados existentes e acrescenta as novas linhas.
- `--inventory`: lista o que foi descoberto sem inicializar o BigQuery.
- `--dry-run`: mostra todos os destinos sem criar datasets/tabelas.
- `--fail-fast`: interrompe no primeiro erro; sem ele, registra a falha e continua.

## Proteções para bases grandes

Cada objeto é carregado primeiro em uma tabela temporária `tabela__loading_<id>`. A tabela oficial só é
publicada por um job de cópia depois que todos os lotes terminam e a quantidade
de linhas da preparação é validada. Uma falha durante a leitura ou carga não
apaga nem deixa incompleta a tabela oficial anterior.

Configuração recomendada para uma VM de produção:

```dotenv
BATCH_SIZE=2000
BATCH_PAUSE_SECONDS=0.2
MAX_RETRIES=3
RETRY_DELAY_SECONDS=2
TABLE_ORDER=smallest
CHECKPOINT_FILE=/opt/migracao/state/migration-checkpoint.json
RESUME=false
MAX_TABLE_BYTES=0
MAX_ROWS_PER_TABLE=0
PG_STATEMENT_TIMEOUT_MS=0
```

- `BATCH_PAUSE_SECONDS` reduz a disputa por recursos entre lotes.
- `MAX_RETRIES` aplica novas tentativas com espera exponencial aos jobs do BQ.
- `MAX_TABLE_BYTES` ignora objetos acima do limite em bytes (`0` desativa).
- `MAX_ROWS_PER_TABLE` limita linhas para testes e publica em `tabela__sample`, preservando a tabela oficial. A amostra não entra no checkpoint.
- `TABLE_ORDER=smallest` conclui primeiro as tabelas menores.
- `PG_STATEMENT_TIMEOUT_MS` limita consultas longas (`0` desativa).
- O checkpoint é gravado atomicamente somente após a publicação de cada tabela.

Para retomar uma execução interrompida sem refazer as tabelas já concluídas:

```bash
sudo -u postgres /opt/migracao/.venv/bin/python \
  /opt/migracao/migrate.py --resume
```

Em uma nova fotografia completa, execute sem `--resume`. Se uma tabela falhar
no meio, ela recomeça desde o início na próxima tentativa; a retomada dentro da
própria tabela exigiria paginação por chave primária, que não pode ser aplicada
genericamente a views ou tabelas sem chave estável.

## Mapeamento principal

| PostgreSQL/PostGIS | BigQuery |
|---|---|
| smallint, integer, bigint | INTEGER |
| real, double precision | FLOAT |
| numeric: até 29 dígitos inteiros e escala até 9 | NUMERIC |
| numeric: até 38 dígitos inteiros e escala até 38 | BIGNUMERIC |
| numeric sem limite ou fora dessas faixas | STRING (preserva o valor) |
| boolean | BOOLEAN |
| date | DATE |
| timestamp sem fuso | DATETIME |
| timestamp com fuso | TIMESTAMP |
| text/varchar/uuid | STRING |
| json/jsonb | JSON |
| bytea | BYTES |
| geometry/geography | GEOGRAPHY |

Arrays e tipos PostgreSQL não reconhecidos são serializados como `STRING` e registrados no log. A etapa futura BigQuery → Bucket não está incluída neste pacote.

## Teste local

```bash
python -m unittest discover -v
```

## Atualização nas VMs

Copie `migrate.py`, `validate_migration.py` e **`migration_common.py` juntos**
para `/opt/migracao`. O módulo compartilhado é obrigatório. Preserve o `.env`
atual; `config/local/env.txt` não é carregado automaticamente. Os exemplos `config/examples/.env.example`
e `config/examples/.env.validacao.example` contêm somente valores de exemplo.

Prepare diretórios graváveis pelo usuário `postgres`:

```bash
sudo install -d -o postgres -g postgres -m 750 \
  /opt/migracao/state /opt/migracao/logs /opt/migracao/validation
```

Configure `CHECKPOINT_FILE=/opt/migracao/state/migration-checkpoint.json`.
O `.env` é carregado ao lado do script, mesmo quando iniciado de outra pasta;
`--env-file /caminho/config` permite escolher outro arquivo. Caminhos relativos
de checkpoint são resolvidos em relação ao diretório desse arquivo.

A descoberta inclui tabelas, partições, views, views materializadas e tabelas
externas. Objetos sem permissão não desaparecem do inventário: sua leitura
falha explicitamente. Pais particionados e suas partições são objetos separados;
não some suas contagens como se fossem conjuntos independentes.

Colisões de nomes normalizados interrompem o banco afetado antes da carga.
Colisões entre nomes de datasets interrompem a execução antes de qualquer carga.
Filtros sem objetos e tabelas ignoradas por tamanho geram saída `1`.

Checkpoints antigos não são aceitos com `--resume`: inicie uma nova rodada sem
essa opção. A retomada verifica se o destino ainda existe e, em `truncate`, se
sua contagem confere com o checkpoint. Ela não detecta mudanças na origem após
a carga; para uma fotografia nova, execute sem `--resume`.

Não execute duas migrações simultâneas para o mesmo destino/checkpoint. Jobs de
cada lote e publicação reutilizam o mesmo ID nas tentativas, mas uma nova
execução em `append` pode duplicar dados se a execução anterior publicou e foi
interrompida antes de salvar o checkpoint. Para recargas completas, use `truncate`.
Falhas abruptas podem deixar tabelas `__loading_<id>` para inspeção/limpeza.

As geometrias são convertidas para 2D antes de produzir WKT (Z/M são removidos).
A validação de contagens, nomes e tipos é descrita em `docs/validacao.md`.
Para rodar todos os testes, instale também `requirements-validation.txt`.
