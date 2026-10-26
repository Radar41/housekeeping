.RECIPEPREFIX = >
.PHONY: run test slurm
run:
> uv run hk $(MOD) $(ARGS)
test:
> uv run pytest -q
slurm:
> mkdir -p logs
> test -f slurm/$(MOD).sbatch || { echo "no slurm/$(MOD).sbatch; render one from templates/slurm (processes/05-rebuild-pipeline.md step 6)"; exit 2; }
> sbatch slurm/$(MOD).sbatch
