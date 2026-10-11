"""Equal-budget Optuna replay over a response table measured by the real slicer."""
import json,time,warnings,sys
from pathlib import Path
import optuna
warnings.filterwarnings('ignore',category=optuna.exceptions.ExperimentalWarning)
optuna.logging.set_verbosity(optuna.logging.ERROR)
data=json.loads(Path(sys.argv[1]).read_text());out={}
for name,record in data.items():
 table={tuple(row['choice']):row for row in record['table']}
 base=table[(15,'grid',.2)]['metrics']
 def score(m):return .6*base['seconds']/m['seconds']+.4*base['total_grams']/m['total_grams']
 trials=[]
 for seed in (1,7,42):
  t=time.perf_counter();study=optuna.create_study(directions=['minimize','minimize'],sampler=optuna.samplers.TPESampler(seed=seed,n_startup_trials=4,multivariate=True))
  evaluated={(15,'grid',.2)}; best={'seconds':base['seconds'],'grams':base['total_grams']};bestscore=1.;asks=0
  while len(evaluated)<record['greedy']['slices'] and asks<200:
   trial=study.ask();key=(trial.suggest_categorical('density',[10,15]),trial.suggest_categorical('pattern',['grid','gyroid','adaptivecubic']),trial.suggest_categorical('layer',[.2,.24,.28]));asks+=1
   row=table[key];evaluated.add(key)
   if row['status']!='success':study.tell(trial,state=optuna.trial.TrialState.FAIL);continue
   m=row['metrics'];study.tell(trial,[m['seconds'],m['total_grams']])
   if score(m)>bestscore+.005:bestscore=score(m);best={'seconds':m['seconds'],'grams':m['total_grams']}
  greedy_score=.6*base['seconds']/record['greedy']['seconds']+.4*base['total_grams']/record['greedy']['grams']
  trials.append({'seed':seed,'unique_evaluations':len(evaluated),'asks':asks,'planner_seconds':time.perf_counter()-t,'best':best,'score':bestscore,'greedy_score':greedy_score,'score_gain_pct':100*(bestscore/greedy_score-1)})
 out[name]={'greedy':record['greedy'],'optuna':trials,'exhaustive_best':max(({'choice':list(key),'score':score(row['metrics']),'seconds':row['metrics']['seconds'],'grams':row['metrics']['total_grams']} for key,row in table.items() if row['status']=='success'),key=lambda x:x['score'])}
 print(name,out[name],flush=True)
Path(sys.argv[2]).write_text(json.dumps(out,indent=2))
