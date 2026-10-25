.RECIPEPREFIX = >
.PHONY: run test slurm
run:
> uv run hk $(MOD) $(ARGS)
test:
> uv run pytest -q
slurm:
> sbatch slurm/$(MOD).sbatch
