# UE Source Exporter

Ferramenta Linux-first para extrair conteúdo que você possui em projetos Unreal
para uma biblioteca-fonte externa. Ela nunca escreve no projeto Godot e nunca
ativa viewport, GPU, bake de materiais ou geração de texturas durante o export.

Ela é inspirada na separação de exportação de geometria e materiais do projeto
[Vortech UnrealToGodot](https://github.com/vortechU/UnrealToGodot), mas este
supervisor é independente e executa cada lote em um processo `UnrealEditor-Cmd`
descartável com `NullRHI`.

## O que funciona agora

- uma lista externa de paths `/Game/...`, um por linha;
- `StaticMesh`, `SkeletalMesh`, `AnimSequence` e `FoliageType` que referencia
  uma `StaticMesh`;
- GLB com malha, skeleton e animação quando a Unreal conseguir exportá-los;
- parâmetros PBR nomeados, texturas-fonte e colisões registrados no manifest;
- três assets por processo, reinício entre lotes, logs por lote e retomada;
- validação estrutural de cada GLB; uma saída incompleta nunca vira sucesso.

Ainda não converte graphos de material Unreal, Blueprints, Niagara, mapas,
colocação de foliage nem terreno. Montanhas e grandes massas de geologia ficam
fora desta ferramenta: o Wave usa Terrain3D para isso.

## Exportar uma seleção

Crie `selecao.txt` com um asset por linha. Comentários após `#` são permitidos:

```text
/Game/Medieval_Castle/Meshes/Props/SM_Arrow
/Game/Characters/Manny/SK_Manny
/Game/Characters/Manny/Animations/Idle  # AnimSequence
```

Execute no terminal:

```bash
python3 bin/ue_source_exporter.py run \
  --unreal-cmd /home/gh/UE5.8.2/Engine/Binaries/Linux/UnrealEditor-Cmd \
  --project '/caminho/Projeto.uproject' \
  --selection /caminho/selecao.txt \
  --textures --max-texture-size 2048 \
  --output /caminho/wave_source/exports/nome_do_bundle
```

O modo padrão usa `NullRHI`, exporta animações e retoma apenas GLBs já validados.
`--textures` é deliberadamente opcional: ele exporta PNGs-fonte separados em
`textures/`, sem bake, e o processo host reduz somente as cópias exportadas ao
limite indicado. O `.uasset` original nunca é alterado. Use `0` para conservar
a resolução integral; use `2048` como padrão seguro para importação no Godot.
Para verificar a seleção sem abrir a Unreal, acrescente `--dry-run`. Para uma
máquina mais fraca, mantenha `--batch-size 3` ou reduza para `1`. Não use
`--with-rhi` como tentativa de corrigir uma falha; ele existe somente para
investigar um asset pequeno que explicitamente precise de RHI.

Cada execução cria `ue_source_export_001.log`, um relatório por lote e o
`ue_source_export_manifest.json` consolidado, com materiais, colisão, animação
e validação dos GLBs. Se um item falhar, os lotes bons permanecem válidos; rode
o mesmo comando novamente para retomar.

## Gerar materiais no Godot

Após revisar a saída no `wave_source`, copie apenas o bundle aprovado para uma
pasta relativa do projeto Godot:

```bash
python3 bin/ue_source_exporter.py stage-godot \
  --manifest /caminho/wave_source/exports/nome_do_bundle/ue_source_export_manifest.json \
  --godot-project /caminho/wave \
  --destination assets_runtime/_staging/nome_do_bundle
```

Em seguida, depois de conferir os arquivos copiados, execute o importador:

```bash
/caminho/godot --headless --path /caminho/wave --import
/caminho/godot --headless --path /caminho/wave \
  --script /caminho/unreal-source-exporter/godot/import_ue_source_bundle.gd -- \
  --bundle-manifest res://assets_runtime/_staging/nome_do_bundle/ue_source_export_manifest.json
```

Ele cria `materials/*.tres` e `scenes/*.tscn` wrappers. O mapeamento padrão é
albedo, normal, emissão e mapas ORM (R=AO, G=roughness, B=metallic); RMA/MRA é
tratado com os canais correspondentes. Revise visualmente cada asset no editor
antes de promovê-lo de `_staging` para o runtime aprovado.

## Verificar repetibilidade

```bash
python3 bin/ue_source_exporter.py compare /saidas/probe_1 /saidas/probe_2
```

O comando compara o conteúdo GLB e ignora bytes de alinhamento permitidos pelo
formato.

## Próximo incremento seguro

Depois da prova com uma `SkeletalMesh` e uma `AnimSequence`, o próximo passo é
reutilizar seletivamente o extrator de parâmetros PBR e a importação de layouts
da Vortech. Texturas continuarão uma fase à parte, com limite de resolução e
validação de memória antes de qualquer exportação grande.
