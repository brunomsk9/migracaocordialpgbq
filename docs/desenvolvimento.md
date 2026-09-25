# Organização e Git

Os scripts permanecem na raiz para preservar os comandos usados nas VMs:

```text
migracaocordial/
├── migrate.py                 # entrada da migração
├── validate_migration.py      # entrada da validação
├── migration_common.py        # regras compartilhadas
├── requirements*.txt          # dependências
├── config/
│   ├── examples/              # exemplos sem credenciais, versionados
│   └── local/                 # configuração real, ignorada pelo Git
├── docs/                      # instruções e revisão técnica
├── tests/                     # testes automatizados
├── tools/build_bundle.py      # empacotamento para as VMs
└── dist/                      # ZIP gerado, ignorado pelo Git
```

O antigo `env.txt` foi preservado em `config/local/env.txt`. Os scripts continuam
lendo `.env` na raiz por padrão; mover esse arquivo histórico não altera a
configuração de execução. Para usar um arquivo específico, informe `--env-file`.
Não copie configurações locais para `config/examples/`.

## Fluxo de trabalho

Na raiz, com o ambiente virtual ativo:

```bash
python -m unittest discover -v
git status --short
git diff
# Selecione explicitamente os arquivos da alteração revisada:
git add caminho/do/arquivo
git diff --cached
git commit -m "Descreve a alteração realizada"
```

A preferência de criar um commit ao concluir cada alteração solicitada está
registrada em `AGENTS.md`. O commit é local. Publicação remota requer configurar
um provedor e um destino; um repositório Git local não é um backup externo.

## Distribuição

```bash
python tools/build_bundle.py
```

Envie `dist/migracao-atualizada.zip` para a VM e extraia seu conteúdo em
`/opt/migracao`, preservando o `.env` existente. O pacote inclui somente uma lista
explícita de código, testes, documentação e exemplos; não inclui configurações
reais, credenciais, `.git`, checkpoints ou dados.
